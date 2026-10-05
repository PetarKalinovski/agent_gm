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
from src.models import Location, NPC, NPCRelationship, Player, WorldClock, get_session, init_db, reset_engine
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
from src.services import party as party_mod
from src.services import dialogue as dialogue_mod
from src.web.dialogue_markup import DialogueMarkupFilter, MetaSentenceFilter, is_command_text, parse_dialogue_line
from src.services.commands import CommandRunner, STATE_TOOLS as COMMAND_STATE_TOOLS, answers_combat_outcome
from src.services.spawn import pick_spawn_location
from src.services.starting_kit import grant_starting_kit
from src.services import encounters
from src.services import job_offers
from src.services import stakes
from src.services import voice_tics
from src.services import world_tick as world_tick_mod
from src.services.combat import build_combat_start
from src.services.rest import normalize_rest, rest_now
from src.services import standing
from src.tools.world_write.items import _validate_inventory, transfer_item
from src.tools.world_write.npcs import move_npc
from src.web.turn_filters import CodeFenceFilter, DialogueDeduper, JunkLineFilter, SentenceDeduper, speech_events_for_unspoken
from src.web.tool_rescue import rescue_tool_calls, resolve_npc
from src.web.turn_audit import audit_turn, format_audit_feedback, mark_object_seen, unseen_scene_objects

# Last turn's audit warnings per player — injected into the next turn's
# context so the DM repairs (or stops making) unbacked claims
_pending_audit_feedback: dict[str, list] = {}
# What the ENGINE did on the DM's behalf last turn (tool calls it wrote as
# text and the rescue path executed) — the DM must not redo or re-narrate it
_pending_engine_notes: dict[str, list[str]] = {}
# How each player's last DM reply's `@commands` landed (failures and notable
# engine answers) — the DM wrote them blind, it hears the answers next turn
_pending_command_ledger: dict[str, list[str]] = {}
# The last combat roster sent to each player that no /api/combat/result has
# answered yet. Seen live: start_combat streamed while the client was still
# loading the new scene, the roster was dropped, and the DM waited for an
# outcome that could never come. The next turn re-issues it instead.
_pending_combat: dict[str, dict] = {}
# How many turns re-issue an unanswered roster before the fight breaks off
MAX_ROSTER_REISSUES = 2

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


def build_player_state(player_id: str, heard_now: str = "") -> dict[str, Any]:
    """Snapshot of everything the HUD needs — sent as an SSE `state` event
    after each turn and included in /api/session.

    Runs after every successful world-write tool, so everything derived here
    is pure dict math over the already-loaded rows (`progression.combat_stats`
    issues no query) plus the campaign summary's handful of indexed reads.

    Its text names people only as far as the player has heard them
    (`dialogue.player_facing_state`; `heard_now` is what this turn said that
    no memory file holds yet) — fleet5: the tracker named "Captain Coldiron"
    while every speaker line still said "Watch Captain".
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
    party: list[dict[str, Any]] = []
    names = dialogue_mod.NameGate.for_player(player_id, heard_now)
    try:
        with get_session() as db:
            row = db.get(Player, player_id)
            lead = lead_mod.compute_lead(db, row) if row else None
            if row is not None:
                # Proof nobody has found is shown as proof, not by name — in
                # the quest log, the tracker and the chip built from it
                # (fleet10-bram T1: "Bring the cracked bone whistle back…")
                unfound = dialogue_mod.unfound_steps(db, row)
                lead = dialogue_mod.hide_unfound(lead, unfound)
                quests = dialogue_mod.hide_unfound(quests, unfound)
                suggestions = suggestions_mod.suggest_actions(db, row, lead, row.current_location_id,
                                                              names=names)
                party = party_mod.party_payload(db, row)
    except Exception as e:
        logger.debug(f"Lead unavailable: {e}")

    hidden = _offstage(player_id)

    # Spoiler-free by construction — never a sealed truth or an unfired beat
    try:
        campaign = campaign_state(player_id)
    except Exception as e:
        logger.debug(f"Campaign state unavailable: {e}")
        campaign = None

    return dialogue_mod.player_facing_state({
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
        "party": party,
        "weather": weather,
        # Who stands here now (alive, on the player's screen): the canvas
        # re-syncs its people when the engine moved someone mid-turn
        "present_ids": [
            n["id"] for n in location.get("npcs_present", [])
            if isinstance(n, dict) and n.get("id") and n["id"] not in hidden
        ],
    }, names)


@app.get("/api/session/{player_id}")
async def get_session_info(player_id: str):
    """Get current session info for a player."""
    get_or_create_session(player_id)

    # Get current state
    location = get_current_location(player_id) # This helper usually returns a dict
    clock = get_world_clock()
    player = get_player(player_id)
    hidden = _offstage(player_id)

    return {
        "player_id": player_id,
        "player_name": player.get("name", "Unknown"),
        "location": location.get("name", "Unknown"),
        "location_id": location.get("id"),
        "location_description": location.get("description", ""),
        "time": f"Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00",
        "time_of_day": clock.get("time_of_day", "day"),
        "npcs_present": dialogue_mod.player_facing_people(player_id, [
            n for n in location.get("npcs_present", [])
            if not (isinstance(n, dict) and n.get("id") in hidden)
        ]),
        "state": build_player_state(player_id),
    }


def _offstage(player_id: str | None) -> set[str]:
    """People the player's screen leaves out of a scene (the story's villain
    before their first scene, `campaign.offstage_ids`). Never raises."""
    try:
        from src.services.campaign import offstage_ids

        with get_session() as db:
            return offstage_ids(db, player_id)
    except Exception as e:
        logger.debug(f"offstage check skipped: {e}")
        return set()


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
        name_cache: dict[str, tuple[str, str | None]] = {}
        # A replayed turn names people the way the live stream did: by what
        # the player has heard (a reload must not undo the name gate)
        replay_text = "\n".join(
            str(b.get("text") or "") for m in messages[-limit:] if m.get("role") == "assistant"
            for b in (m.get("content") or []) if isinstance(b, dict)
        )
        names = dialogue_mod.NameGate.for_turn(player_id, "", context=replay_text)

        for msg in messages[-limit:]:  # Get last N messages
            role = msg.get("role", "unknown")
            content_blocks = msg.get("content", [])

            # Extract text content from message blocks
            text_content = ""
            for block in content_blocks:
                if isinstance(block, dict):
                    # Direct text content
                    if "text" in block and "toolUse" not in block and "reasoningContent" not in block:
                        piece = block.get("text", "")
                        # Two text blocks are two thoughts (a tool call or a
                        # stripped reasoning block sat between them): never
                        # glue "tonight." to "Something shifts"
                        if text_content and piece and not text_content[-1].isspace() and not piece[0].isspace():
                            text_content += "\n\n"
                        text_content += piece

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
                    parts = final_content.rsplit("Player says:", 1)
                    if len(parts) > 1:
                        final_content = parts[1].strip()
                    # A new turn: a name said in the last scene is not said in this one
                    names.next_turn(final_content)

                entry: dict[str, Any] = {"role": role, "content": final_content}
                if role == "assistant":
                    # The reply as the player saw it: prose and speaker lines
                    # in order, command lines gone
                    entry["parts"] = _reply_parts(final_content, name_cache, names,
                                                  getattr(dm, "_player_name", None))
                    entry["content"] = "\n".join(
                        p["text"] for p in entry["parts"] if p["kind"] == "text"
                    ).strip()
                    if not entry["parts"]:
                        continue
                chat_history.append(entry)

        return {"messages": chat_history}

    except Exception as e:
        logger.error(f"Error getting chat history: {e}", exc_info=True)
        return {"messages": [], "error": str(e)}


def _reply_parts(text: str, name_cache: dict[str, tuple[str, str | None]],
                 names: Any = None, player_name: str | None = None) -> list[dict[str, Any]]:
    """A stored DM reply split the way the player saw it streamed.

    Command lines (`@give …`) were never shown; speaker lines were speech
    (portrait, name); everything else is prose. Consecutive prose lines merge.
    With a `names` gate, speakers and prose name people as the player knows
    them (the stored reply is the DM's raw text, real names and all).
    """
    from src.web.dialogue_markup import is_player_label

    parts: list[dict[str, Any]] = []
    speakers_by_label: dict[str, str | None] = {}     # for the player's-name cut only
    # The gate reads the whole history: only a cut in THIS reply retracts its
    # prose about the name (fleet9-tamsin T11, `NameGate.comments_on_cut_name`)
    cuts_before = len(getattr(names, "player_name_cuts", None) or [])
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if is_command_text(line):
            continue
        parsed = parse_dialogue_line(line) if line.strip() else None
        if parsed and is_player_label(parsed["speaker"], player_name):
            # The player's own voice was dropped live, too; the narration
            # after the quote was kept
            if not parsed.get("remainder"):
                continue
            line, parsed = parsed["remainder"], None
        if parsed and parsed.get("quoted"):
            label = parsed["speaker"]
            if label not in name_cache:
                try:
                    with get_session() as db:
                        npc = dialogue_mod.resolve_speaker(db, label)
                        if npc is None and names is not None:
                            # The dead resolve among nobody: a villain killed
                            # before anyone said his name must not get it back
                            # on a reload
                            npc = next((n for n in db.query(NPC).filter(NPC.status != "alive").all()
                                        if (n.name or "").lower() == label.lower()), None)
                        shown = (names.known_as(npc) if names is not None else npc.name) if npc else label
                        name_cache[label] = (shown, npc.id) if npc else (label, None)
                except Exception:
                    name_cache[label] = (label, None)
            name, npc_id = name_cache[label]
            said = parsed["text"]
            if names is not None and hasattr(names, "player_name_in"):
                # ...and without the player's name from someone who never
                # heard it, as it streamed (a reload must not put Piper's
                # "Straight lines, Bram" back: fleet7-bram T16). A role or
                # trade label was resolved among the people present then;
                # here it is whoever alive it can only be
                if npc_id is None and label not in speakers_by_label:
                    speakers_by_label[label] = _speaker_anywhere(label)
                said = names.player_name_in(npc_id or speakers_by_label.get(label), said, name)
            parts.append({"kind": "speech", "npc_name": name, "npc_id": npc_id,
                          "text": said, "tone": parsed.get("tone"), "action": parsed.get("action")})
            if parsed.get("remainder"):
                parts.append({"kind": "text", "text": parsed["remainder"]})
            continue
        if parts and parts[-1]["kind"] == "text":
            parts[-1]["text"] += "\n" + line
        else:
            parts.append({"kind": "text", "text": line})
    cut_here = len(getattr(names, "player_name_cuts", None) or []) > cuts_before
    for part in parts:
        if part["kind"] == "text":
            part["text"] = part["text"].strip("\n")
            if names is not None:
                part["text"] = names.mask(part["text"])
            # The model's bookkeeping was dropped live; the stored reply still
            # has it, and a reload must not show it (fleet8-tamsin T9/T13)
            meta = MetaSentenceFilter(player_name=player_name,
                                      also_drop=names.comments_on_cut_name if cut_here else None)
            part["text"] = (meta.feed(part["text"]) + meta.flush()).strip("\n")
    return [p for p in parts if p["kind"] != "text" or p["text"].strip()]


def _speaker_anywhere(label: str) -> str | None:
    """The id of the one living person a role or trade label can be
    ("Stable Hand", "Barkeep"), with everyone alive standing in for the room
    it was said in; None for a voice nobody answers to. Never raises."""
    try:
        with get_session() as db:
            alive = [row[0] for row in db.query(NPC.id).filter(NPC.status == "alive").all()]
            npc = dialogue_mod.resolve_speaker(db, label, alive)
            return npc.id if npc is not None else None
    except Exception as e:
        logger.debug(f"replay speaker {label!r} unresolved: {e}")
        return None


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


@app.get("/api/journal/{player_id}")
async def get_journal(player_id: str):
    """The character's journal: what they have learned, oldest first."""
    from src.services import memory_files

    init_db(get_active_db_path())
    return {"entries": await asyncio.to_thread(memory_files.read_journal, player_id)}


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


def _speech_from_markup(parsed: dict[str, Any], present_ids: list[str],
                        player_id: str | None = None, names: Any = None, *, where: str | None = None,
                        left_from: str | None = None, before: str = "") -> dict[str, Any]:
    """A speech event from a parsed speaker line, speaker resolved to an NPC.

    "Coldiron" becomes Captain Brann Coldiron (people in the room first);
    a label that names nobody real stays an ambient voice ("Gate Guard").
    A stranger speaking under their role label ("Ash-Cloaked Man") is shown
    under it — the name is the player's to learn (2026-10-04: chips and
    labels told the player the villain's name before anyone said it). The
    raw `label` rides along: the first line under their real name is the
    introduction (src/services/dialogue.record_turn_dialogue).

    With the turn's `names` gate (dialogue.NameGate) a name must also have
    been SAID: the line is heard first, then the speaker is shown by the
    part of their name the player has heard ("Vex", not "Vex Marrow"), and
    a real-name label nobody said is shown — and recorded — as the role
    label (fleet4: "Wolf-Mantled Man" -> "Aldric Vane" with nobody saying
    it). `_introduces` is the gate's verdict for the caller to pass on.

    A label naming someone absent the engine must not believe came — the
    story's cast, a merchant, someone seen elsewhere today — is a name in
    use borrowed for a new person (fleet10-wenna T14, "Hesk:" for a cart
    driver; dialogue.borrowed_speaker): `where` (the line's place), the
    reply's `left_from` and the prose `before` it decide; the line goes out
    under the role the prose gave the speaker, filed nowhere, and
    `_borrowed` tells the audit.
    """
    label = parsed.get("speaker") or ""
    note: dict[str, Any] = {
        "type": "speech",
        "npc_name": label,
        "label": label,
        "text": parsed.get("text"),
        "tone": parsed.get("tone") or "normal",
        "action": parsed.get("action"),
    }
    if parsed.get("continues"):
        note["continues"] = True            # the rest of a long line (dialogue_markup.split_long_speech)
    if names is not None:
        # whoever said it, the player heard it — a long line cut in two,
        # whole (`line`), so both bubbles name the speaker alike
        names.hear(parsed.get("line") or parsed.get("text"))
    try:
        with get_session() as db:
            npc = dialogue_mod.resolve_speaker(db, label, present_ids)
            why = dialogue_mod.borrowed_speaker(db, player_id, npc, label, present_ids, where,
                                                left_from=left_from, before=before) if npc is not None else None
            if why:
                here_row = db.get(Location, where)
                # the same voice keeps the role it first went out under
                role = dialogue_mod.borrowed_role(db, player_id, label, where) or dialogue_mod.role_from_prose(
                    before, db.query(NPC).filter(NPC.id.in_(list(present_ids or []))).all())
                dialogue_mod.note_borrowed(player_id, label, where, role)
                note["_borrowed"] = {"npc_id": npc.id, "name": npc.name, "label": label, "role": role, "why": why,
                                     "there": npc.current_location.name if npc.current_location else "elsewhere",
                                     "here": here_row.name if here_row is not None else "here"}
                note["npc_name"] = note["label"] = role
                logger.info(f"Name in use borrowed: {label!r} is not {npc.name} ({why}) — shown as {role!r}")
                npc = None
            if npc is not None:
                note["npc_id"] = npc.id
                if names is not None:
                    verdict = names.speaker(npc, label)
                    note["npc_name"] = verdict["shown"]
                    note["label"] = verdict["label"]
                    note["_introduces"] = verdict["introduces"]
                else:
                    met = dialogue_mod.player_has_met(db, player_id, npc.id) if player_id else True
                    note["npc_name"] = (dialogue_mod.role_label(npc)
                                        if not met and dialogue_mod.is_role_label(label, npc) else npc.name)
        if names is not None and note.get("action"):
            note["action"] = names.mask(note["action"])
    except Exception as e:
        logger.debug(f"speaker resolution failed: {e}")
    if names is not None:
        # The player's name, from someone who never heard it, is cut before
        # the bubble, the voice, the memory file or the transcript take the
        # line (fleet7: Piper's "Straight lines, Bram", "…them, Tamsin —")
        note["text"] = names.player_name_in(note.get("npc_id"), note.get("text"), note.get("npc_name") or label)
    return note


def _present_at(present_ids: list[str], location_id: str | None) -> list[str]:
    """The brief's people, then whoever stands at `location_id` (where a
    mid-reply @move took the player). Never raises."""
    if not location_id:
        return present_ids
    try:
        with get_session() as db:
            there = [row[0] for row in db.query(NPC.id).filter(
                NPC.current_location_id == location_id, NPC.status == "alive").all()]
    except Exception as e:
        logger.debug(f"people at {location_id} unreadable: {e}")
        return present_ids
    return list(dict.fromkeys([*present_ids, *there]))


def _quests_started(results: list[Any]) -> list[str]:
    """Ids of the quests this reply's commands started. A @quest-start or
    @quest-new restating one already active started nothing, and must not
    hold back its talk_to steps."""
    return [r.result["id"] for r in results or []
            if r.ok and r.tool in ("activate_quest", "create_quest") and isinstance(r.result, dict)
            and r.result.get("id") and not r.result.get("already_active") and not r.result.get("exists")]


def _known_as(names: Any, npc_id: str | None, fallback: str | None) -> str | None:
    """What the player calls this NPC right now (the turn's NameGate)."""
    try:
        with get_session() as db:
            npc = db.get(NPC, npc_id) if npc_id else None
            return names.known_as(npc) if npc is not None else fallback
    except Exception as e:
        logger.debug(f"display name failed for {npc_id}: {e}")
        return fallback


def _name_and_voice_warnings(request: Any, names: Any, markup: DialogueMarkupFilter, runner: Any,
                             spoken: list[dict[str, Any]], turn_text: str) -> list[dict[str, str]]:
    """The stream's name and voice decisions as audit warnings: names nobody
    said (`unintroduced_name`), the player's own voice (`voiced_the_player`),
    the player's name cut from a stranger's line (`name_before_introduction`),
    and people who exist nowhere (`unknown_named_person`, read off the DM's
    speaker lines and @journal entries). Never raises."""
    from src.web import turn_audit as turn_audit_mod

    out: list[dict[str, str]] = []
    try:
        journal = [str(r.result.get("journal")) for r in runner.results
                   if r.ok and isinstance(r.result, dict) and r.result.get("journal")]
        # The journal is on the player's screen too: a name nobody said is a
        # leak there as well (fleet4-wenna T17 journaled "Sorrel Tack" a turn
        # before he gave his surname)
        for entry in journal:
            names.mask(entry)
        for warning in (turn_audit_mod.unintroduced_name_warning(names.unintroduced, names.masked),
                        turn_audit_mod.voiced_player_warning(markup.player_lines),
                        turn_audit_mod.player_name_cut_warning(getattr(names, "player_name_cuts", []))):
            if warning:
                out.append(warning)
        texts = [str(n.get("text") or "") for n in spoken] + journal
        if any(t.strip() for t in texts):
            with get_session() as db:
                known, common = dialogue_mod.world_words(db)
            # A word the reply or the player writes in lowercase is a word
            seen = "\n".join([turn_text, request.player_input or ""] + texts)
            common |= {w.lower() for w in re.findall(r"[\w'’-]+", seen) if w[:1].islower()}
            warning = turn_audit_mod.unknown_person_warning(
                turn_audit_mod.unknown_named_people(texts, known, common))
            if warning:
                out.append(warning)
    except Exception as e:
        logger.warning(f"name/voice audit failed: {e}")
    return out


def _arrival_scene_frames(player_id: str) -> list[str]:
    """The `scene` event for where the player now stands (a location header in
    the chat, the music's time-of-day cue). describe_location used to send it;
    a `@move` that lands does now, straight from the Location row."""
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            location = db.get(Location, player.current_location_id) if player and player.current_location_id else None
            if location is None:
                return []
            clock = db.query(WorldClock).first()
            payload = {
                "type": "scene",
                "location": location.name,
                "time_of_day": clock.get_time_of_day() if clock else "day",
                "atmosphere": list(location.atmosphere_tags or []),
                "description": location.description or "",
            }
        return [f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"]
    except Exception as e:
        logger.debug(f"arrival scene event failed: {e}")
        return []


def _absent_speakers(player_id: str, spoken: list[dict[str, Any]],
                     left_from: str | None = None) -> list[str]:
    """Named NPCs who spoke this turn but are not where the player is.

    The reply format cannot force a @move. Playtest 2026-10-04: the DM walked
    the player "to the square" in prose and voiced Sorrel there, with no
    @move — the player stayed in the tavern and the canvas said so. A speaker
    who is somewhere else (and not travelling in the party) is the tell.

    `left_from`: where the player stood when the turn began. Someone still
    standing there when the reply's @move took the player away was saying
    goodbye — the commands come first, so the farewell is stamped after the
    move (fleet5-bram T7 Sorrel's "You be careful out there", fleet5-wenna T19).
    """
    out: list[str] = []
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            if player is None:
                return out
            party = set(party_mod.member_ids(player))
            moved_from = left_from if left_from and left_from != player.current_location_id else None
            seen: set[str] = set()
            for note in spoken or []:
                npc_id = note.get("npc_id")
                if not npc_id or npc_id in seen or npc_id in party:
                    continue
                seen.add(npc_id)
                npc = db.get(NPC, npc_id)
                stood = note.get("_where") or player.current_location_id
                if npc is None or npc.current_location_id in (stood, player.current_location_id):
                    continue
                if moved_from and npc.current_location_id == moved_from:
                    continue                    # a goodbye from the place they just left
                where = npc.current_location.name if npc.current_location is not None else "somewhere else"
                here_row = db.get(Location, stood) if stood else None
                here = here_row.name if here_row is not None else "nowhere"
                out.append(f"{npc.name} spoke, but they are at {where} and the player is at {here}")
    except Exception as e:
        logger.debug(f"absent-speaker check failed: {e}")
    return out


def _bring_speakers_here(player_id: str, spoken: list[dict[str, Any]],
                         left_from: str | None = None) -> list[str]:
    """Move the people the reply voiced beside the player to where the player is.

    fleet6-tamsin T13-19: the DM hung a sign on the Watch post ("Drowned
    Lantern, evenings") and voiced Coldiron at the tavern hearth with no
    @npc-move. speaker_not_here told it to @move the PLAYER, who was already
    there; it rewrote that no-op @move and Coldiron stayed in the square for
    four turns. When the player's own place is not in doubt (the caller skips
    this when the prose narrates an arrival without a @move), a person who
    speaks at their side IS at their side: the engine follows the story, the
    way an @npc-move would. Never the campaign villain or successor (the
    story stages them), the dead, a companion, or a goodbye from the place
    the player just left. Returns "<name> → <place>" for each move.
    """
    from src.models.campaign import Campaign

    moves: list[tuple[str, str, str, str]] = []
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            if player is None or not player.current_location_id:
                return []
            party = set(party_mod.member_ids(player))
            staged: set[str] = set()
            for campaign in db.query(Campaign).filter(Campaign.status.in_(("pending", "active"))).all():
                staged.update(i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i)
            moved_from = left_from if left_from and left_from != player.current_location_id else None
            seen: set[str] = set()
            for note in spoken or []:
                npc_id = note.get("npc_id")
                if not npc_id or npc_id in seen or npc_id in party or npc_id in staged:
                    continue
                seen.add(npc_id)
                stood = note.get("_where") or player.current_location_id
                if stood != player.current_location_id:
                    continue                    # spoken before the reply's @move landed
                npc = db.get(NPC, npc_id)
                if npc is None or (npc.status or "alive") != "alive":
                    continue
                if npc.current_location_id in (None, stood):
                    continue
                if moved_from and npc.current_location_id == moved_from:
                    continue                    # a goodbye from the place they just left
                # Never someone the story needs where they stand, a merchant
                # at their counter, or anyone the player saw elsewhere today:
                # fleet10-wenna T14 walked the market's hawker to the
                # Gatehouse because a made-up driver was called by his first
                # name (speaker_not_here asks the DM instead)
                kept = dialogue_mod.kept_in_place(db, player_id, npc, stood)
                if kept:
                    logger.info(f"Speaker not brought here: {npc.name} ({kept})")
                    continue
                here = db.get(Location, stood)
                if here is None:
                    continue
                moves.append((npc.id, npc.name, here.id, here.name))
    except Exception as e:
        logger.debug(f"bring-speakers check failed: {e}")
        return []
    done: list[str] = []
    for npc_id, name, place_id, place_name in moves:
        try:
            result = move_npc(npc_id=npc_id, destination_id=place_id)
        except Exception as e:
            logger.warning(f"Could not bring {name} to {place_name}: {e}")
            continue
        if not result.get("error"):
            done.append(f"{name} → {place_name}")
    return done


# A thing's words in one sentence of the prose: two of its content words, or
# its one word when its label has just one ("Notice board" -> notice, board)
_THING_STOP = frozenset({"the", "a", "an", "of", "on", "in", "at", "to", "and", "with", "from", "old"})


def _shows_thing(prose: str, label: str) -> bool:
    words = {w for w in re.findall(r"[a-z]+", (label or "").lower()) if w not in _THING_STOP and len(w) >= 4}
    if not words:
        return False
    need = 1 if len(words) == 1 else 2
    for sentence in re.split(r"[.!?\n]+", (prose or "").lower()):
        if len(words & set(re.findall(r"[a-z]+", sentence))) >= need:
            return True
    return False


def _arrives_at(prose: str, name: str) -> bool:
    """Prose that puts the player AT a place it names: "you reach Wolf's
    Hollow", "Wolf's Hollow opens below you" — not "the road to it", not
    "it lies an hour ahead"."""
    place = re.escape(name.replace("’", "'")).replace("'", "['’]")
    text = prose or ""
    return bool(
        re.search(rf"\byou(?:['’]re| are)?\s+(?:arrive|reach|come|step|emerge|climb|crest|top|make it|"
                  rf"find yourself|stand)\b[^.!?\n]{{0,80}}?\b{place}", text, re.IGNORECASE)
        or re.search(rf"\b{place}\b[^.!?\n]{{0,30}}\b(?:opens?|spreads?|sprawls?|falls? away)\b"
                     rf"[^.!?\n]{{0,15}}\b(?:below|before|beneath|around)\s+you", text, re.IGNORECASE)
    )


def _carried_on_to(player_id: str, prose: str, left_from: str | None = None) -> dict[str, Any] | None:
    """The neighbouring place the reply's prose put the player in while the
    database has them where its last @move (or no @move) left them.

    fleet7-tamsin T9: `@move North Gatehouse | through`, then the prose
    walked on — "Wolf's Hollow opens below you" — and laid the Hollow's
    cracked bone whistle at her feet. The database kept her at the
    gatehouse: the whistle she pocketed never reached her pack, and every
    later line about it failed. A place next to where they stand (a road
    or a doorway away), named in the prose AND arrived at or shown by its
    own Things, is where the story put them. Returns {"id", "name",
    "blocked", "requirements", "shown"} or None."""
    if not prose:
        return None
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            here_id = player.current_location_id if player is not None else None
            here = db.get(Location, here_id) if here_id else None
            if here is None:
                return None
            from src.models import Connection

            neighbours: set[str] = set()
            for conn in db.query(Connection).filter(
                (Connection.from_location_id == here_id) | (Connection.to_location_id == here_id)
            ).all():
                if conn.from_location_id == here_id:
                    neighbours.add(conn.to_location_id)
                elif conn.bidirectional:
                    neighbours.add(conn.from_location_id)
            if here.parent_id:
                neighbours.add(here.parent_id)
            neighbours |= {row[0] for row in db.query(Location.id).filter(Location.parent_id == here_id).all()}
            neighbours -= {here_id, left_from, None}
            flat = (prose or "").replace("’", "'").lower()
            for place in db.query(Location).filter(Location.id.in_(neighbours)).all():
                name = (place.name or "").strip()
                if len(name) < 4 or name.replace("’", "'").lower() not in flat:
                    continue
                if str(getattr(place.type, "value", place.type)).lower() == "town":
                    continue                    # a whole town is never a place one stands in
                shown = [o["label"] for o in normalize_objects(place.objects) if o.get("kind") != "body"
                         and _shows_thing(prose, o["label"])]
                if not shown and not _arrives_at(prose, name):
                    continue
                route = check_route(db, here_id, place.id, player)
                return {"id": place.id, "name": name, "from": here.name, "blocked": bool(route.get("blocked")),
                        "requirements": list(route.get("requirements") or []), "shown": shown}
    except Exception as e:
        logger.debug(f"carried-on check failed: {e}")
    return None


# The story holding the player where they stand: a hand on their sleeve,
# "before you've gone three steps" (fleet8-wenna T3), someone stopping them
# or planting herself "directly in your way before you can escape toward the
# tavern" (fleet4-tamsin T12; fleet5-tamsin T3 "before you can take more than
# a step" — replayed, both were walked off mid-conversation)
_HELD_BACK = re.compile(
    r"\bcatch(?:es)?\s+(?:at\s+)?your\s+(?:sleeve|arm|elbow|wrist|hand|shoulder|cloak|coat)\b"
    r"|\b(?:stops?|halts?|blocks?|bars?)\s+(?:you|your\s+(?:way|path))\b"
    r"|\b(?:cuts?|steps?|plants?\s+(?:herself|himself|themselves))\s+(?:across|into|in)\s+your\s+(?:path|way)\b"
    r"|\b(?:directly|right|squarely)\s+in\s+your\s+(?:path|way)\b"
    r"|\bbefore\s+you(?:['’]ve|\s+have)?\s+(?:gone|taken|made\s+it|got(?:ten)?|reached?)\b"
    r"|\bbefore\s+you\s+(?:can|could)\b"
    r"|\bholds?\s+you\s+(?:back|there|up|in\s+place)\b"
    r"|\byou\s+(?:stop|halt|stay|turn\s+back)\b",
    re.IGNORECASE,
)
# ...a hold that is none ("nothing stops you", "He doesn't move to block
# you", fleet8-tamsin T20)
_NO_HOLD = re.compile(r"(?:\b(?:nothing|nobody|no\s+one|none|never|not)|n['’]t)(?:\s+[\w'’]+){0,3}?\s*$",
                      re.IGNORECASE)
# The place named as where they are headed, not where they stand
_HEADED = re.compile(r"\b(?:toward|towards|for|to)\s+(?:the\s+)?(?:[\w'’-]+\s+){0,2}$", re.IGNORECASE)


def _at_place(sentence: str, name: str) -> bool:
    """The sentence names the place — whole, or by a long last word ("the
    Gatehouse") — as where they ARE ("the guard stops you under the
    Gatehouse arch"), not only where they are going ("before you can escape
    toward the tavern")."""
    flat = (sentence or "").replace("’", "'").lower()
    words = re.findall(r"[a-z']+", (name or "").lower())
    if not words:
        return False
    forms = [" ".join(words)] + ([words[-1]] if len(words[-1]) >= 6 else [])
    return any(not _HEADED.search(flat[:m.start()])
               for form in forms for m in re.finditer(rf"\b{re.escape(form)}\b", flat))


def _held_short(prose: str, name: str) -> bool:
    """Did the story stop the player short of where they set out for? A hold
    that says they never got there ("before you've gone three steps",
    "before you can") holds wherever the place is named — fleet7-bram T3 said
    "The gatehouse is three minutes north" two sentences on, and the place
    named anywhere used to excuse it; any other hold, unless its own sentence
    has them at the place. Prose that has them arrive after all holds
    nothing."""
    words = re.findall(r"[A-Za-z'’]+", name or "")
    if _arrives_at(prose, name) or (words and len(words[-1]) >= 6 and _arrives_at(prose, words[-1])):
        return False
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", prose or ""):
        for m in _HELD_BACK.finditer(sentence):
            if _NO_HOLD.search(sentence[:m.start()]):
                continue
            if m.group(0).lower().startswith("before") or not _at_place(sentence, name):
                return True
    return False


# A reply that showed the shut way stopping them did what the line asked
# (the guard turning them back, the gate barred) — unless it walked them
# through anyway ("The gate swings wide enough to let you through",
# fleet8-tamsin T12)
_SHOWN_SHUT = re.compile(
    r"\b(?:barred|shut|closed|locked|refus\w*|not\s+tonight|won['’]?t\s+open|stays?\s+(?:shut|closed|barred)"
    r"|turns?\s+you\s+(?:back|away)|no\s+(?:one|way)\s+(?:through|past|out))\b",
    re.IGNORECASE,
)
_PASSED = re.compile(
    r"\b(?:swings?|swung|creaks?|grinds?|groans?)\s+(?:wide|open)\b|\blets?\s+you\s+(?:through|pass|out)\b"
    r"|\bwaves?\s+you\s+(?:through|on|past)\b|\byou(?:['’]re|\s+are)\s+through\b"
    r"|\byou\s+(?:pass|slip|step|walk)\s+(?:through|beneath|under|past)\s+(?:the\s+)?(?:gate|arch|bar)"
    r"|\byou\b[^.!?\n]{0,40}\b(?:over|past|beyond|through)\s+the\s+(?:wall|walls|gate|gates|palisade)\b",
    re.IGNORECASE,
)


def _unshown_for_audit(dropped: list[str] | None, player_name: str | None) -> str:
    """The sentences the bookkeeping filter dropped, the player's name made
    "you", for the hand-over checks only (turn_audit.audit_turn `unshown`)."""
    from src.web.dialogue_markup import to_second_person

    out = []
    for sentence in dropped or []:
        try:
            out.append(to_second_person(sentence, player_name) if player_name else sentence)
        except Exception:
            out.append(sentence)
    return "\n".join(out)


def _hand_over_answered(warning: dict[str, Any]) -> bool:
    """An audit warning that says the player received a thing that never
    reached the pack — answered once the engine gave it (intents.taken_fallback)."""
    detail = str(warning.get("detail") or "")
    return warning.get("check") == "item_without_tool" or (
        warning.get("check") == "item_handover_without_command"
        and ("into the player's hands" in detail or detail.startswith("Narration slides")))


def _walk_as_said(player_id: str, context: str, runner: Any, left_from: str | None, prose: str,
                  roll_outcomes: list[str], *, fought: bool = False, spoken_text: str = "") -> dict[str, Any] | None:
    """The player set out for a place, the reply's binding travel line said
    to take them there, and the reply wrote no @move.

    fleet8-tamsin T11: "I'm already moving toward the North Gatehouse… I head
    for the gate" got the line; the reply staged the gate ("The North
    Gatehouse rises ahead", a guard speaking there) with no @move, and the
    database kept her in the square for two replies. As with a job the
    player took ([START THIS JOB]), the engine does it: the move a @move
    makes, the road's time included, told in the next HOW YOUR LAST
    COMMANDS LANDED. Never through a shut way (an audit line says where
    they still are and what stops them — unless the reply showed that stop
    itself, as the line asked), nor when the story held them short of it (a
    hand on their sleeve, `_held_short`), a roll failed, or a fight broke
    out — then the audit line says they are still where they were. A reply
    that slept (@rest) is left alone: its night is over.

    Returns {"ledger": line} after a move, {"audit": warning}, or None."""
    from src.services import travel_intent

    line = travel_intent.issued(context)
    # A reply that slept (@rest) ended in the morning: never a walk after it
    if line is None or not left_from or any(r.verb in ("move", "rest") for r in runner.results):
        return None
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            if player is None or player.current_location_id != left_from:
                return None             # something else moved them (a paid bed's @rest)
            here = db.get(Location, left_from)
            place = db.query(Location).filter(Location.name == line["place"]).first()
            if here is None or place is None or place.id == here.id:
                return None
            route = check_route(db, here.id, place.id, player)
            here_name, place_id, place_name = here.name, place.id, place.name
            from src.services.travel import night_why

            tonight = night_why(route, player)
    except Exception as e:
        logger.debug(f"travel fallback check failed: {e}")
        return None

    def still_here(detail: str) -> dict[str, Any]:
        return {"audit": {"check": "travel_not_taken", "detail": detail}}

    if line["shut"] and not route.get("blocked"):
        return None                     # the reply opened the way (a pass handed over): theirs to take next
    if route.get("blocked"):
        from src.services.travel import BARRED_TONIGHT

        # (a guard's own words count: "Gate Guard: 'Barred till first light'")
        if (_SHOWN_SHUT.search(f"{prose or ''}\n{spoken_text or ''}") or _held_short(prose, place_name)) \
                and not _PASSED.search(prose or ""):
            return None                 # it showed what stops them, as the line asked: nothing to correct
        why = tonight or "; ".join(str(x) for x in route.get("requirements") or [] if x != BARRED_TONIGHT) \
            or "the way is shut"
        past = (f"only a won roll or a bribe gets them through tonight — then @move {place_name} | through first; "
                "otherwise they wait for first light" if route.get("barred_tonight") else
                f"only if the fiction truly gets them past it, write @move {place_name} | through first")
        return still_here(
            f"The player set out for {place_name}, but the way is shut to them ({why}). They are still at "
            f"{here_name}: nothing past it happened. Open your next reply where they are and show what stops "
            f"them; {past}.")
    held = _held_short(prose, place_name)
    if fought or held or any(o in ("failure", "critical_failure") for o in roll_outcomes or []):
        return still_here(
            f"The player said they go to {place_name}, and your reply wrote no @move, so they are still at "
            f"{here_name}. If the story let them go, write @move {place_name} first in your next reply; if "
            "something stopped them, open the next reply with it — never narrate them there without the @move.")
    from src.services.commands import _spend_road
    from src.tools.world_write.player import move_player as move_player_tool

    try:
        moved = move_player_tool(player_id=player_id, destination_id=place_id)
    except Exception as e:      # the rest of the turn's audit still runs
        logger.warning(f"engine walk to {place_name} failed: {e}")
        moved = {}
    if not moved.get("success"):
        return still_here(
            f"The player said they go to {place_name}, but they are still at {here_name} — the way would "
            "not take them. Open your next reply where they are.")
    try:
        _spend_road(runner, left_from, place_id, place_name)
    except Exception as e:      # the walk stands; only its minutes are lost
        logger.debug(f"road time for the engine's walk not spent: {e}")
    logger.info(f"Travel line honoured by the engine: {here_name} -> {place_name}")
    return {"ledger": (f"(engine) @move {place_name}: the player said they go to {place_name} and your reply "
                       f"wrote no @move, so the engine walked them there, as they said. They are at {place_name} "
                       "now — open your next reply there.")}


# Prose that takes someone along with the player: "falls in beside you",
# "walks you to the yard", "leads the way", "you're both past the well" —
# and "falls into step beside you" (fleet9-tamsin T7, only "falls in step"
# was read), "he follows a few steps after" (fleet9-bram T10), "keeps pace at
# your shoulder" (fleet9-tamsin T16), "hovers a step behind you" (fleet6-tamsin
# T13): replayed over every transcript's @move turns
_ALONG = re.compile(
    r"\b(?:falls?|fell|falling)\s+in\s+(?:beside|alongside|with|step|behind)\b"
    r"|\b(?:falls?|fell|falling)\s+into\s+step\b"
    r"|\b(?:follows?|followed|following)\s+(?:you\b|close\s+behind|(?:a\s+)?(?:few\s+)?(?:steps?|paces?)\s+"
    r"(?:after|behind))"
    r"|\b(?:keeps?|kept|keeping)\s+pace\s+at\s+your\s+(?:side|shoulder|elbow)\b"
    r"|\ba\s+(?:step|pace|half[\s-]step)\s+behind\s+you\b"
    r"|\b(?:walks?|walked|walking|leads?|led|leading|escorts?|escorted|escorting|guides?|guided|guiding"
    r"|takes?|took|taking|brings?|brought|bringing)\s+you\s+(?:to|toward|towards|over|across|up|down|out"
    r"|through|along|back|into|the\s+(?:rest\s+of\s+the\s+)?way)\b"
    r"|\b(?:walks?|walked|walking|comes?|came|coming|goes|went|going|keeps?\s+pace|kept\s+pace|keeping\s+pace"
    r"|strides?|strode|striding)\s+(?:along\s+)?(?:with|beside|alongside)\s+you\b"
    r"|\b(?:walks?|walking|stays?|staying|keeps?|keeping|remains?)\s+at\s+your\s+(?:side|shoulder|elbow)\b"
    r"|\b(?:leads?|led|leading)\s+the\s+way\b"
    # "you're both" going somewhere — never "you're both of a mind"
    r"|\byou(?:['’]re|\s+are|\s+were)\s+both\s+(?:past|through|out|inside|outside|across|beyond|clear|away|under"
    r"|into|in\s+the|at\s+the|on\s+the\s+road|moving|walking|heading)\b"
    r"|\b(?:you\s+both|the\s+two\s+of\s+you)\s+(?:walk|head|make|move|go|cross|set|step|reach|pass|leave|slip|push"
    r"|climb|arrive|come|went|start)\w*\b"
    r"|\b(?:walk\w*|strid\w*|strode|mov\w*|go|goes|going|went)\s+side\s+by\s+side\b",
    re.IGNORECASE,
)
_BOTH_OF_YOU = re.compile(r"you(?:['’]re|\s+are|\s+were)\s+both|you\s+both|the\s+two\s+of\s+you|side\s+by\s+side",
                          re.IGNORECASE)
# ...and prose that leaves them behind ("doesn't follow", "stays put", "watches
# you both go", "walks you to the edge of the square and stops" — never "her
# grip doesn't loosen until you're both through the square")
_LEFT_BEHIND = re.compile(
    r"\b(?:does\s*n['’]?t|does\s+not|did\s*n['’]?t|did\s+not|won['’]?t|will\s+not|never|no\s+longer|not)\s+"
    r"(?:[\w'’]+\s+){0,2}?(?:follow|follows|come|comes|go|goes|walk|walks|join|joins|move|moves|budge|leave)\b"
    r"|\bstays?\s+(?:behind|put|where)\b"
    r"|\bwatch(?:es|ing|ed)?\s+(?:you|the\s+two\s+of\s+you)(?:\s+both)?\s+(?:go|leave|walk|depart|head|set\s+off)\b"
    r"|\b(?:sees?|saw|waves?|waved|sends?|sent)\s+you\s+off\b|\bsends?\s+you\s+on\s+your\s+way\b"
    r"|\bas\s+far\s+as\b|\bto\s+the\s+edge\s+of\b|\band\s+(?:stops|halts|turns\s+back|goes\s+back|heads\s+back)\b"
    r"|\b(?:leaves?|left)\s+you\s+(?:there|at|to)\b|\bparts?\s+(?:ways|from\s+you)\b",
    re.IGNORECASE,
)
_SUBJECT_PRONOUN = re.compile(r"\b(?:he|she|they)\b", re.IGNORECASE)
_PLAIN_HEADS = frozenset({"hand", "man", "woman", "boy", "girl", "folk", "stranger", "figure", "person"})
# "…pausing when he catches sight of the two of you crossing the yard"
# (fleet9-tamsin T7): whoever SEES the two of you is not one of them
_WATCHER = re.compile(r"\b(?:sight\s+of|watch\w*|sees?|seeing|saw|spots?|spotted|eyes?|eyed|eyeing|between|at)\s*$",
                      re.IGNORECASE)
# "her eyes following you", "his gaze goes with you": the look goes, not them
_LOOK_SUBJECT = re.compile(
    r"\b(?:his|her|their|its)\s+(?:[\w'’-]+\s+)?(?:eyes?|gaze|glance|look|stare|voice|words?|attention|thoughts?"
    r"|warning|laugh|shadow)\s*$", re.IGNORECASE)
# ...and "follows you with her eyes" (the idiom), or a going-along still to
# come ("she'll walk you to the yard tomorrow", "offers to walk you over"):
# nobody went anywhere this reply
_LOOK_AFTER = re.compile(r"^[^.!?;]{0,25}?\bwith\s+(?:his|her|their|its)\s+(?:[\w'’-]+\s+)?(?:eyes|gaze|stare|look"
                         r"|glance)\b", re.IGNORECASE)
_NOT_YET = re.compile(
    r"(?:\b(?:will|would|could|should|might|may|must|can|cannot|if|unless|offers?\s+to|offered\s+to|means?\s+to"
    r"|wants?\s+to|promises?\s+to|promised\s+to|about\s+to|going\s+to|ready\s+to)|['’]ll|['’]d)(?:\s+[\w'’]+)?\s*$",
    re.IGNORECASE)
# A line called after the player from the place they left is a goodbye,
# however far the prose has walked them ("Behind you, the Keeper calls from
# the doorway"): it never brings the caller along
_CALLED_AFTER = re.compile(
    r"\b(?:calls?|called|calling|shouts?|shouted|shouting|yells?|yelled|yelling|cries|cried|crying)\s+(?:out\s+)?"
    r"(?:after|behind)\s+you\b"
    r"|\bfrom\s+(?:the|her|his|their)\s+(?:[\w'’-]+\s+)?(?:door|doorway|threshold|porch|window|stoop)\b"
    r"|\bbehind\s+you\s*,\s*(?:from|back|still)\b", re.IGNORECASE)


def _mention(npc: Any, label: str) -> re.Pattern | None:
    """Words that refer to this person in the prose: their name's words, and
    the role label the player knows them by (whole, or by its head noun
    when that is no everyday word — "the Captain", never "hand")."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’-]+", npc.name or "") if len(w) >= 4]
    terms = [re.escape(w) for w in words]
    label_words = re.findall(r"[A-Za-z][A-Za-z'’-]+", label or "")
    if label_words:
        terms.append(r"\s+".join(re.escape(w) for w in label_words))
        if label_words[-1].lower() not in _PLAIN_HEADS and len(label_words[-1]) >= 4:
            terms.append(re.escape(label_words[-1]))
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE) if terms else None


def _people_patterns(people: list[Any]) -> dict[str, dict[str, Any]]:
    """{npc_id: {"pattern", "pronoun"}} for NPC rows (inside their session):
    what the prose calls them, and the pronoun that may stand for them."""
    from src.services.dialogue import role_label

    pronoun_of = getattr(dialogue_mod, "pronoun", None)
    out: dict[str, dict[str, Any]] = {}
    for npc in people:
        pattern = _mention(npc, role_label(npc))
        if pattern is not None:
            out[npc.id] = {"pattern": pattern, "pronoun": (pronoun_of(npc) if pronoun_of else "") or ""}
    return out


def _last_mentioned(people: dict[str, dict[str, Any]], text: str, pronoun: str = "", owners: bool = True,
                    skip: tuple = ()) -> str | None:
    """The person named last in `text`. "he" is never the woman named just
    before him; and "the Captain's warning goes with you" is his warning, not
    him (owners=False) — though "Coldiron's jaw sets; he falls in beside
    you" is him."""
    found = [(m.end(), npc_id) for npc_id, p in people.items() if npc_id not in skip
             for m in p["pattern"].finditer(text or "")
             if (owners or text[m.end():m.end() + 2].replace("’", "'") != "'s")
             and (pronoun not in ("he", "she") or p.get("pronoun") in ("", pronoun))]
    return max(found)[1] if found else None


def _voiced(people: dict[str, dict[str, Any]], spoken: list[dict[str, Any]] | None, pronoun: str = "",
            skip: tuple = ()) -> str | None:
    """The last of these people to speak in the reply (a "she" it never
    names in prose is the one doing the talking), matching `pronoun`."""
    for note in reversed(spoken or []):
        npc_id = note.get("npc_id")
        if npc_id in people and npc_id not in skip and (
                pronoun not in ("he", "she") or people[npc_id].get("pronoun") in ("", pronoun)):
            return npc_id
    return None


_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")


def _along_ids(prose: str, people: dict[str, dict[str, Any]], spoken: list[dict[str, Any]] | None = None, *,
               dest_id: str | None = None, dest_name: str = "") -> list[str]:
    """Who of `people` (where the player was) the reply took along, in order.

    A going-along phrase (`_ALONG`) whose subject is them — named, by role
    label, or a pronoun standing for someone named just before (or, never
    named in this reply's prose, for the one of them who spoke). "The two
    of you" / "you both" carry no pronoun of their own: the sentence's
    "he" is whoever watched them go (fleet9-tamsin T7). And someone from
    there voiced at the new place (`_where`) after the prose had the player
    there (`_at`, `_at_place`) came along — a goodbye is said before the
    new place is shown (fleet5-bram T7's "You be careful out there") —
    unless the prose left them behind, or they called after the player.

    The unnamed "she" is the one who talks on the way, never a goodbye said
    before the going-along sentence; and "the two of you" alone never
    reaches for a speaker — the other one may be a companion or someone at
    the new place, while the voice from the place left was a farewell."""
    if not prose or not people:
        return []
    sentences = [s for s in _SENTENCES.split(prose) if s.strip()]
    starts: list[int] = []
    pos = 0
    for s in sentences:
        k = prose.find(s, pos)
        starts.append(max(k, 0))
        pos = k + len(s) if k >= 0 else pos
    going: list[str] = []
    behind: set[str] = set()
    for i, sentence in enumerate(sentences):
        left = _LEFT_BEHIND.search(sentence)
        if left:
            stayed = _last_mentioned(people, sentence[:left.start()], owners=False)
            if stayed:
                behind.add(stayed)
            continue
        m = _ALONG.search(sentence)
        if m is None:
            continue
        before = sentence[:m.start()]
        if _LOOK_SUBJECT.search(before) or _LOOK_AFTER.search(sentence[m.end():]) or _NOT_YET.search(before):
            continue
        both = bool(_BOTH_OF_YOU.search(m.group(0)))
        watcher = _last_mentioned(people, before, owners=False) if both and _WATCHER.search(before) else None
        skip = (watcher,) if watcher else ()
        who = None if both else _last_mentioned(people, before, owners=False)
        subject = [] if both else _SUBJECT_PRONOUN.findall(before)
        if who is None and (subject or both):
            pronoun = subject[-1].lower() if subject else ""
            for previous in ([] if watcher else [before]) + list(reversed(sentences[max(0, i - 2):i])):
                who = _last_mentioned(people, previous, pronoun, skip=skip)
                if who:
                    break
            if who is None and not both:
                talking = [n for n in spoken or [] if not isinstance(n.get("_at"), int) or n["_at"] >= starts[i]]
                who = _voiced(people, talking, pronoun, skip=skip)
        if who and who not in going:
            going.append(who)
    if dest_name:
        for note in spoken or []:
            npc_id, at = note.get("npc_id"), note.get("_at")
            if (npc_id in people and npc_id not in going and npc_id not in behind and dest_id
                    and note.get("_where") == dest_id and isinstance(at, int) and _at_place(prose[:at], dest_name)
                    and not _called_after(prose[:at], note)):
                going.append(npc_id)
    return going


def _called_after(before: str, note: dict[str, Any]) -> bool:
    """Was this line called after the player from the place they left — its
    gesture ("calling after you"), or the prose sentence just before it
    ("Behind you, the Keeper calls from the doorway")? (`_CALLED_AFTER`)"""
    pieces = [s for s in _SENTENCES.split((before or "").rstrip()) if s.strip()]
    said = " ".join(str(note.get(k) or "") for k in ("action", "tone"))
    return bool(_CALLED_AFTER.search(said) or (pieces and _CALLED_AFTER.search(pieces[-1])))


def _walked_along(player_id: str, prose: str, left_from: str | None,
                  spoken: list[dict[str, Any]] | None = None) -> list[str]:
    """Move the people the prose walked along with the player, after a reply
    that moved the player. Returns a ledger line per move.

    fleet8-bram T2: "He says nothing until you're both past the well… the
    Gatehouse's torches swimming into view", @move North Gatehouse, and the
    Captain spoke there — the database kept him in the square all session,
    so T6 had him "still near the well where you left him". Speakers the
    player left behind are excused as goodbyes (_bring_speakers_here), so
    only the prose can say who came along: a going-along phrase whose
    subject is them (named, by role label, or "he"/"you're both" right after
    they were). The engine moves them — the audit line alone is what the DM
    ignored in round 6, and later prose follows the database — through the
    same tool as @npc-move, said in the next ledger so the DM can send them
    back. Never the campaign villain or successor (the story stages them), a
    companion (the party follows by itself), the dead, or into a place the
    map's own way does not open.

    fleet9-tamsin T7: "She falls into step beside you" and "…catches sight
    of the two of you crossing the yard", and Piper spoke in the yard — she
    stayed in the square three turns. `spoken` (this reply's speech, each
    with `_where` it was said and `_at`, how much prose came before it)
    names the "she" the reply never names, and someone from the place left
    who speaks once the prose has the player at the new place came along
    (`_along_ids`)."""
    if not prose or not left_from:
        return []
    from src.models.campaign import Campaign

    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            if player is None or not player.current_location_id or player.current_location_id == left_from:
                return []
            dest, origin = db.get(Location, player.current_location_id), db.get(Location, left_from)
            if dest is None or origin is None or check_route(db, left_from, dest.id, None).get("blocked"):
                return []
            party = set(party_mod.member_ids(player))
            staged: set[str] = set()
            for campaign in db.query(Campaign).filter(Campaign.status.in_(("pending", "active"))).all():
                staged.update(i for i in (campaign.villain_npc_id, campaign.successor_npc_id) if i)
            people = [n for n in db.query(NPC).filter(NPC.current_location_id == left_from).all()
                      if (n.status or "alive") == "alive" and n.id not in party and n.id not in staged]
            at_origin = _people_patterns(people)
            names = {n.id: n.name for n in people}
            dest_id, dest_name, origin_name = dest.id, dest.name, origin.name
    except Exception as e:
        logger.debug(f"walked-along check failed: {e}")
        return []
    if not at_origin:
        return []
    going = _along_ids(prose, at_origin, spoken, dest_id=dest_id, dest_name=dest_name)
    lines: list[str] = []
    for npc_id in going:
        try:
            result = move_npc(npc_id=npc_id, destination_id=dest_id)
        except Exception as e:
            logger.warning(f"Could not walk {names[npc_id]} along to {dest_name}: {e}")
            continue
        if isinstance(result, dict) and not result.get("error"):
            lines.append(f"(engine) {names[npc_id]} walked along with the player to {dest_name} in your story, "
                         f"so the engine moved them there too. If they turned back, write @npc-move "
                         f"{names[npc_id]} | {origin_name}.")
    return lines


# Prose that sends someone off: "ducks back off toward the Stable Yard at a
# trot" (fleet9-bram T12), "heads for the Drowned Lantern", "slips back into
# the yard" — the place named after it (`_off_to`)
_LEAVES = re.compile(
    r"\b(?:heads?|headed|heading|goes|went|ducks?|ducked|ducking|hurries|hurried|hurrying|slips?|slipped"
    r"|slipping|trots?|trotted|trotting|jogs?|jogged|jogging|strides?|strode|striding|stalks?|stalked|stomps?"
    r"|stomped|walks?|walked|limps?|limped|scurries|scurried|marches|marched|runs|ran|dashes|dashed|bolts?"
    r"|bolted|hustles?|hustled|retreats?|retreated|withdraws?|withdrew|disappears?|disappeared|vanishes"
    r"|vanished|returns?|returned|leaves|sets?\s+off|peels?\s+off|peeled\s+off|makes?\s+(?:her|his|their)\s+way"
    r"|made\s+(?:her|his|their)\s+way|is\s+gone|was\s+gone|['’]s\s+gone)\b",
    re.IGNORECASE,
)
# ...never the player going ("as you head for the yard", "tells you to go"),
# a leaving to come or refused ("before he heads", "won't go to", "he'll
# go"), a look, or the player going with them ("…and you follow")
_PLAYER_GOES = re.compile(
    r"\b(?:you|we)\b(?:\s+(?:both|two|all))?(?:\s+(?!(?:and|then|but|while)\b)[\w'’]+)?\s*$", re.IGNORECASE)
_NOT_GONE = re.compile(
    r"(?:\b(?:not|never|will|would|could|should|might|may|must|can|before|until|unless|if|once|about\s+to"
    r"|ready\s+to|going\s+to|wants?\s+to|means?\s+to|has\s+to|have\s+to|needs?\s+to|tries\s+to)"
    r"|n['’]t|['’]ll|['’]d)(?:\s+[\w'’]+)?\s*$", re.IGNORECASE)
_YOU_GO_TOO = re.compile(r"\byou\s+(?:\w+\s+)?(?:follow|trail|tag\s+along|keep\s+up|go\s+after)\b"
                         r"|\b(?:you\s+both|the\s+two\s+of\s+you)\b", re.IGNORECASE)
# ...and after the leaving verb, "hurries to the Stable Yard with you at his
# heels": theirs is the walk along, never a leaving that splits them up (but
# "Sorrel, standing beside you, ducks back toward the Stable Yard" goes)
_WITH_YOU = re.compile(r"\b(?:with|beside|alongside)\s+you\b|\bat\s+your\s+(?:side|heels|shoulder|elbow)\b",
                       re.IGNORECASE)
# ...nor a fetch ("ducks into the Drowned Lantern and comes back with two
# mugs", "…Lantern. A minute later she's back"), nor a going told of the past
# ("Hesk, who went to the Lantern an hour ago, …"): they are here now
_BACK_AGAIN = re.compile(
    r"\b(?:comes?|came|coming|is|was|['’]s)\s+back\b|\b(?:returns?|returned|returning|reappears?|reappeared"
    r"|re-?emerges?|re-?emerged|emerges?|emerged)\b|\b(?:a\s+)?(?:moments?|minutes?|breath|heartbeat)\s+later\b",
    re.IGNORECASE)
_TOLD_OF = re.compile(r"\b(?:ago|earlier|yesterday|last\s+night|this\s+morning)\b", re.IGNORECASE)
_RELATIVE = re.compile(r"\b(?:who|which)\s*$", re.IGNORECASE)
# Place words that name their place in lower case too ("back to the yard")
_PLACE_NOUNS = frozenset({"square", "yard", "gatehouse", "stables", "hollow", "market", "inn", "tavern", "chapel",
                          "barracks", "docks", "smithy", "mill", "forge", "temple", "harbour", "harbor"})


def _off_to(tail: str, places: dict[str, tuple[str, str]]) -> str | None:
    """The neighbouring place the words after a leaving verb send them to:
    "back off toward the Stable Yard", "for the Gatehouse". `places` maps a
    lower-case form to (place id, form kind: "name" or "word")."""
    if not places:
        return None
    forms = "|".join(re.escape(f) for f in sorted(places, key=len, reverse=True))
    # A short gap, no clause break: "heads for the trough, then glances
    # toward the Stable Yard" goes to the trough
    m = re.match(
        r"[^.!?;:,—–\n]{0,30}?\b(?:back\s+|off\s+|away\s+|out\s+|across\s+|over\s+|down\s+|up\s+|home\s+)*"
        r"(?:toward|towards|to|into|for|through|in\s+the\s+direction\s+of)\s+(?:the\s+)?"
        rf"(?P<place>{forms})(?![\w'’-])", (tail or "").replace("’", "'"), re.IGNORECASE)
    if not m or re.search(r"\b(?:you|your|us)\b", m.group(0)[:m.start("place")], re.IGNORECASE):
        return None                     # "walks you to the yard" takes the player too
    said = m.group("place")
    place_id, kind = places[said.lower().replace("’", "'")]
    # "toward the lantern light" is no Drowned Lantern: a bare last word is
    # the place when written as a name, or when it is a place word
    if kind == "word" and not said[:1].isupper() and said.lower() not in _PLACE_NOUNS:
        return None
    return place_id


# The leaving verb's own clause, and someone else its subject: "He steps
# back, already turning to the next worry in his ledger, and the runner jogs
# off toward the Stable Yard" (fleet10-tamsin T10) — the runner went, and the
# engine took "He" two clauses back for him and walked the Captain to the
# stables. A clause opens after a comma, a dash, "and", "but", "then"…
_CLAUSE_OPEN = re.compile(r"[,;:—–]|\b(?:and|but|then|while|as|so)\b", re.IGNORECASE)
_OWN_SUBJECT = re.compile(r"^\s*(?:(?:the|a|an|another|one|two|three|some|his|her|their)\s+[\w'’-]+"
                          r"|(?-i:[A-Z])[\w'’-]+)", re.IGNORECASE)
_PREPOSITION_FIRST = re.compile(r"^\s*(?:with|without|in|at|on|for|after|before|under|over|into|through|from|by"
                                r"|toward|towards|across|along|past|behind|beside)\b", re.IGNORECASE)


def _someone_else_goes(before: str, people: dict[str, dict[str, Any]]) -> bool:
    """Is the subject of the leaving verb's own clause a noun phrase that is
    none of `people` ("the runner", "Dell") — no pronoun, no one of them?"""
    breaks = list(_CLAUSE_OPEN.finditer(before or ""))
    clause = before[breaks[-1].end():] if breaks else before
    if not clause.strip() or _SUBJECT_PRONOUN.search(clause) or _PREPOSITION_FIRST.match(clause):
        return False
    return _last_mentioned(people, clause, owners=False) is None and bool(_OWN_SUBJECT.match(clause))


def _left_ids(prose: str, people: dict[str, dict[str, Any]], places: dict[str, tuple[str, str]],
              spoken: list[dict[str, Any]] | None = None) -> list[tuple[str, str]]:
    """(npc id, place id) for each of `people` (where the player stands) the
    prose sent off to a neighbouring place: a leaving verb (`_LEAVES`) whose
    subject is them — named, by role label, or a pronoun for someone named
    just before (or for the one of them who spoke) — and a place of
    `places` after it. Never when the verb's own clause has another subject
    (`_someone_else_goes`)."""
    if not prose or not people or not places:
        return []
    sentences = [s for s in _SENTENCES.split(prose) if s.strip()]
    gone: list[tuple[str, str]] = []
    for i, sentence in enumerate(sentences):
        if _YOU_GO_TOO.search(sentence) or _ALONG.search(sentence):
            continue
        for m in _LEAVES.finditer(sentence):
            place_id = _off_to(sentence[m.end():], places)
            if place_id is None:
                continue
            before = sentence[:m.start()]
            if _PLAYER_GOES.search(before) or _NOT_GONE.search(before) or _LOOK_SUBJECT.search(before) \
                    or _RELATIVE.search(before) or _someone_else_goes(before, people):
                continue
            after = sentence[m.end():]
            if _BACK_AGAIN.search(after) or _TOLD_OF.search(after) or _WITH_YOU.search(after) or (
                    i + 1 < len(sentences) and _BACK_AGAIN.search(sentences[i + 1])):
                continue
            who = _last_mentioned(people, before, owners=False)
            subject = _SUBJECT_PRONOUN.findall(before)
            # ...and a "he" whose own sentence (or the one before) spoke of
            # someone else stands for them: "Coldiron grunts. Behind him a
            # runner turns, and he heads off toward the Stable Yard" walked
            # the Captain off (review probe, fleet10-tamsin T10's twin)
            if who is None and subject and not _LINE_ABOUT_SOMEONE.search(before):
                pronoun = subject[-1].lower()
                for previous in reversed(sentences[max(0, i - 2):i]):
                    who = _gone_one(people, previous, pronoun)
                    if who or _LINE_ABOUT_SOMEONE.search(previous):
                        break
                if who is None and not any(_LINE_ABOUT_SOMEONE.search(p) for p in sentences[max(0, i - 2):i]):
                    who = _voiced(people, spoken, pronoun)
            if who and all(who != w for w, _ in gone):
                gone.append((who, place_id))
    return gone


# Someone the story says has ALREADY gone — told after the fact, in the prose
# or in a line, with no @npc-move: fleet10-wenna T17 "the spot by the far
# post where the Drover stood is empty", and Sorrel's "He left right after
# the runner came through … walked off toward the north lane". The Drover
# stood at the fence in the database all evening — on the canvas, with a
# talk chip. "Left"/"gone" only before a stop or a word of when ("left right
# after", "gone since noon"): "gone quiet", "left the cart" are no leaving
_GONE_REPORT = re.compile(
    r"(?P<subj>\b(?:he|she)\b|[\w'’-]+)(?:['’]s|\s+(?:has|had|is|was))?(?:\s+(?:already|just|long|clean))?\s+"
    r"(?P<verb>(?:left|gone)(?=\s*(?:[.,;:!?—–…)\"”]|$)|\s+(?:right|just|already|an?\s+hour|hours|a\s+while"
    r"|earlier|ago|this\s+morning|last\s+night|before|after|when|as\s+soon|without|for\s+good|toward|towards"
    r"|soon|since)\b)"
    r"|went\s+off|walked\s+off|took\s+off|cleared\s+out|headed\s+off|slipped\s+(?:off|away)|wandered\s+off"
    r"|lit\s+out|packed\s+up\s+and\s+(?:left|went))\b", re.IGNORECASE)
_EMPTY_SPOT = re.compile(
    r"\bwhere\s+(?P<subj>[^,.;:!?]{2,60}?)\s+(?:had\s+)?(?:stood|sat|was|waited|leaned|lounged|crouched|lingered"
    r"|had\s+been)\b[^.!?;]{0,50}?\b(?:is|was|lies|lay|stands|sits)\s+(?:now\s+|already\s+)?"
    r"(?:empty|bare|vacant|deserted)\b"
    r"|(?P<owner>[\w'’-]+)['’]s\s+(?:stool|spot|place|seat|post|corner|bench|chair)[^.!?;]{0,30}?\b(?:is|sits"
    r"|stands|lies)\s+(?:now\s+)?empty\b", re.IGNORECASE)
# ...and a leaving that only tells a time: "the howls starting right when he
# left", "ever since she left"
_WHEN_CLAUSE = re.compile(r"\b(?:when|after|since|as\s+soon\s+as|while|because|till|until|the\s+day|the\s+night"
                          r"|the\s+moment)\s*$", re.IGNORECASE)
# A line's "he left" is about whoever the line itself is about: fleet2-wenna
# T13's stable hand told of "this fella at the north wall … then he walked
# off toward the north road", and the prose's last "he" before the line was
# "Coldiron's distant back" — the Captain, standing right there, would have
# been sent home (review replay with every speaker resolved). "That
# stranger? He…", "My brother? He left this morning", "The man in grey —
# he's gone" are someone else
_LINE_ABOUT_SOMEONE = re.compile(
    r"\b(?:the|a|an|this|that|some|my|his|her|our|their|your)\s+(?:[a-z][\w'’-]*\s+){0,2}?"
    r"(?:man|men|woman|fella|fellow|stranger|figure|boy|girl|lad|lass|kid|brother|sister|son|daughter|father"
    r"|mother|husband|wife|cousin|uncle|aunt|friend|rider|driver|carter|drover|runner|guard|watchman|soldier"
    r"|hunter|traveller|traveler|pilgrim|peddler|one)\b", re.IGNORECASE)
# ...and whoever the prose shows doing something now ("Coldiron listens") is
# here, whatever a line says of "him"
_DOING_NOW = re.compile(r"^(?:\s+\w+ly)?\s+(?!(?:is|was|has|does|stands?\s+empty)\b)[a-z]{2,}s\b")


def _shown_here_now(pattern: re.Pattern, prose: str) -> bool:
    """Does the prose show this person doing something in the present
    ("Coldiron listens", "the Captain frowns") outside a told-gone spot?"""
    text = _EMPTY_SPOT.sub(" ", prose or "")
    return any(text[m.end():m.end() + 2].replace("’", "'") != "'s" and _DOING_NOW.match(text[m.end():])
               for m in pattern.finditer(text))


def _gone_one(people: dict[str, dict[str, Any]], text: str, pronoun: str, *, skip: tuple = (),
              owners: bool = True) -> str | None:
    """The one of `people` a "he"/"she" after `text` stands for: named last
    in it — unless someone else is spoken of after them ("The Captain sent
    a runner. He left an hour ago" is the runner)."""
    who = _last_mentioned(people, text, pronoun, owners=owners, skip=skip)
    if who is None:
        return None
    end = max((m.end() for m in people[who]["pattern"].finditer(text or "")), default=0)
    return None if _LINE_ABOUT_SOMEONE.search(text or "", end) else who


def _named_at(people: dict[str, dict[str, Any]], text: str, end: int) -> str | None:
    """The one of `people` whose mention ends exactly at `end` in `text`."""
    for npc_id, p in people.items():
        if any(m.end() == end for m in p["pattern"].finditer(text or "")):
            return npc_id
    return None


def _gone_ids(prose: str, people: dict[str, dict[str, Any]], spoken: list[dict[str, Any]] | None = None,
              player_input: str = "") -> list[tuple[str, str, str]]:
    """(npc id, the words, what follows them) for each of `people` (where
    the player stands) the reply reports as already gone (`_GONE_REPORT`,
    `_EMPTY_SPOT`) — in the prose, or in a line ("He left right after…",
    "he" being whom the prose before the line named last, never the
    speaker). Not when they speak again after it, nor "if/before he left",
    "right when he left" (a time, fleet2-bram T3: the ash-gray man, not
    anyone here), nor "left, but came back". `player_input` is unused: the
    player asking after someone is no one leaving."""
    if not people:
        return []
    found: list[tuple[str, str, str]] = []
    notes = [n for n in spoken or [] if isinstance(n, dict)]
    # (text, where its words sit in the prose, speaker, its index among the lines)
    texts: list[tuple[str, int, str | None, int]] = [(prose or "", 0, None, -1)]
    for index, note in enumerate(notes):
        at = note.get("_at")
        if note.get("text") and isinstance(at, int):
            texts.append((str(note["text"]), at, note.get("npc_id"), index))
    for text, at, speaker, index in texts:
        sentences = [s for s in _SENTENCES.split(text) if s.strip()]
        pos = 0
        for i, sentence in enumerate(sentences):
            k = text.find(sentence, pos)
            pos = k + len(sentence) if k >= 0 else pos
            where = (k if speaker is None and index < 0 and k >= 0 else at)
            hits: list[tuple[str | None, str, str]] = []
            for m in _EMPTY_SPOT.finditer(sentence):
                if m.group("subj"):
                    who = _last_mentioned(people, m.group("subj"), owners=False)
                else:
                    who = _named_at(people, sentence[:m.end("owner")], m.end("owner"))
                hits.append((who, m.group(0), sentence[m.end():]))
            for m in _GONE_REPORT.finditer(sentence):
                before = sentence[:m.start()]
                if _NOT_GONE.search(before) or _RELATIVE.search(before) or _WHEN_CLAUSE.search(before) \
                        or _BACK_AGAIN.search(sentence[m.end():]) \
                        or (i + 1 < len(sentences) and _BACK_AGAIN.search(sentences[i + 1])):
                    continue
                subj = re.sub(r"['’]s$", "", m.group("subj"))          # "he's gone", "Drover's gone"
                end = m.start("subj") + len(subj)
                if subj.lower() in ("he", "she"):
                    pronoun = subj.lower()
                    if speaker is None:
                        # the nearest sentence that names someone: one of
                        # them, or somebody else ("Behind him a runner turns,
                        # and he's gone" is the runner, never the Captain a
                        # sentence back)
                        who = _gone_one(people, before, pronoun)
                        if who is None and not _LINE_ABOUT_SOMEONE.search(before):
                            for previous in reversed(sentences[max(0, i - 2):i]):
                                who = _gone_one(people, previous, pronoun)
                                if who or _LINE_ABOUT_SOMEONE.search(previous):
                                    break
                    else:
                        # a line's "he": someone the line names, else the
                        # one the prose's sentence just before it names —
                        # never someone the line is about ("that stranger?
                        # he…", `_LINE_ABOUT_SOMEONE`), a possessive ("toward
                        # Coldiron's distant back"), or anyone the prose
                        # shows here doing something now
                        skip = (speaker,) if speaker else ()
                        said_before = text[:k + m.start()] if k >= 0 else before
                        who = _gone_one(people, said_before, pronoun, owners=False, skip=skip)
                        if who is None and not _LINE_ABOUT_SOMEONE.search(said_before):
                            last = [s for s in _SENTENCES.split((prose or "")[:at]) if s.strip()][-1:]
                            who = _last_mentioned(people, last[0] if last else "", pronoun, owners=False,
                                                  skip=skip)
                            if who and _shown_here_now(people[who]["pattern"], prose):
                                who = None
                else:
                    who = _named_at(people, sentence[:end], end)
                # what follows, to the end of the line: "…walked off toward
                # the Stable Yard" names where they went
                hits.append((who, m.group(0), " ".join(sentences[i:])[len(sentence[:m.end()]):]))
            for who, words, tail in hits:
                if not who or who == speaker or any(who == w for w, _, _ in found):
                    continue
                # ...and speaking again after it, they are here after all
                if any(n.get("npc_id") == who and (j > index if index >= 0 else
                                                   isinstance(n.get("_at"), int) and n["_at"] > where)
                       for j, n in enumerate(notes)):
                    continue
                found.append((who, words, tail))
    return found


def _walked_off(player_id: str, prose: str, spoken: list[dict[str, Any]] | None = None, *,
                results: list[Any] | None = None, player_input: str = "",
                audits: list[dict[str, str]] | None = None) -> list[str]:
    """Move the people the prose sent off to a neighbouring place, after the
    reply. Returns a ledger line per move.

    Never a move the reply's own @npc-move already made, to that place or of
    that person (`results`; fleet10-tamsin T10 wrote `@npc-move Dell |
    Stable Yard`). Someone the reply reports as already gone (`_gone_ids`)
    goes too — to the neighbouring place the words name, else home; when
    home is here, `audits` gets left_but_present asking for the @npc-move.

    fleet9-bram T12: "He nods once, decisive, and ducks back off toward the
    Stable Yard at a trot" with no @npc-move — Sorrel stood in the square
    in the database all evening while the story had him at the stables. The
    mirror of `_walked_along`: who the prose sends away goes, through the
    same tool as @npc-move, and the next ledger says so. Never a companion
    (the party is the player's), the dead, a beast (the encounters keep
    theirs), anyone the story keeps here (`world_tick.kept_by_story`: the
    villain and successor, the people of a beat due now or armed here), or
    along a way the map does not open."""
    if not prose:
        return []
    from src.models import Connection
    from src.services.dialogue import is_beast
    from src.services.world_tick import kept_by_story

    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            here_id = player.current_location_id if player is not None else None
            here = db.get(Location, here_id) if here_id else None
            if here is None:
                return []
            party = set(party_mod.member_ids(player))
            kept = kept_by_story(db, player_id, here_id)
            people = [n for n in db.query(NPC).filter(NPC.current_location_id == here_id).all()
                      if (n.status or "alive") == "alive" and n.id not in party and n.id not in kept
                      and not is_beast(n)]
            present = _people_patterns(people)
            if not present:
                return []
            neighbours: set[str] = set()
            for conn in db.query(Connection).filter(
                    (Connection.from_location_id == here_id) | (Connection.to_location_id == here_id)).all():
                if conn.from_location_id == here_id:
                    neighbours.add(conn.to_location_id)
                elif conn.bidirectional:
                    neighbours.add(conn.from_location_id)
            if here.parent_id:
                neighbours.add(here.parent_id)
            neighbours |= {row[0] for row in db.query(Location.id).filter(Location.parent_id == here_id).all()}
            places: dict[str, tuple[str, str]] = {}
            place_names: dict[str, str] = {}
            for place in db.query(Location).filter(Location.id.in_(neighbours - {here_id, None})).all():
                if str(getattr(place.type, "value", place.type)).lower() == "town":
                    continue                    # a whole town is never a place one walks off into
                if check_route(db, here_id, place.id, None).get("blocked"):
                    continue
                words = re.findall(r"[A-Za-z][A-Za-z'’-]*", (place.name or "").replace("’", "'"))
                if not words or len(" ".join(words)) < 4:
                    continue
                place_names[place.id] = place.name
                places[" ".join(words).lower()] = (place.id, "name")
                if words[0].lower() == "the" and len(words) > 1:
                    places.setdefault(" ".join(words[1:]).lower(), (place.id, "name"))
                last = words[-1].lower()
                if len(words) > 1 and (len(last) >= 5 or last in _PLACE_NOUNS):
                    places.setdefault(last, (place.id, "word"))
            names = {n.id: n.name for n in people}
            here_name = here.name
            homes = {n.id: (n.home_location_id, db.get(Location, n.home_location_id).name)
                     for n in people if n.home_location_id and n.home_location_id != here_id
                     and db.get(Location, n.home_location_id) is not None}
    except Exception as e:
        logger.debug(f"walked-off check failed: {e}")
        return []
    # What the reply's own @npc-move lines already moved, and where to
    moved_names = {str((r.result or {}).get("npc_name") or "") for r in results or []
                   if getattr(r, "ok", False) and getattr(r, "verb", "") == "npc-move" and isinstance(r.result, dict)}
    moved_to = {str((r.result or {}).get("to_location") or "") for r in results or []
                if getattr(r, "ok", False) and getattr(r, "verb", "") == "npc-move" and isinstance(r.result, dict)}
    lines: list[str] = []
    gone: list[tuple[str, str]] = []
    for npc_id, place_id in _left_ids(prose, present, places, spoken):
        if names[npc_id] in moved_names or place_names.get(place_id) in moved_to:
            logger.info(f"Walk-off skipped: the reply's @npc-move already moved someone to "
                        f"{place_names.get(place_id)}")
            continue
        gone.append((npc_id, place_id))
    reported: list[tuple[str, str, str | None]] = []
    for npc_id, words, tail in _gone_ids(prose, present, spoken, player_input):
        if names[npc_id] in moved_names or any(npc_id == g for g, _ in gone):
            continue
        reported.append((npc_id, words, _off_to(tail, places) or (homes.get(npc_id) or (None,))[0]))
    for npc_id, words, place_id in reported:
        if place_id is None:
            logger.info(f"Reported gone, nowhere to send: {names[npc_id]} ({words!r})")
            if audits is not None:
                audits.append({"check": "left_but_present", "detail": (
                    f"Your story says {names[npc_id]} has gone (\"{words[:80]}\"), but the world still has them "
                    f"at {here_name} — on the player's screen, ready to talk. Write @npc-move {names[npc_id]} | "
                    "<where they went> first in your next reply (@new-place it if it is somewhere new), or show "
                    "them still here.")})
            continue
        if place_id not in place_names:
            place_names[place_id] = (homes.get(npc_id) or ("", "their home"))[1]
        gone.append((npc_id, place_id))
    told = {npc_id: words for npc_id, words, _ in reported}
    for npc_id, place_id in gone:
        try:
            result = move_npc(npc_id=npc_id, destination_id=place_id)
        except Exception as e:
            logger.warning(f"Could not send {names[npc_id]} off to {place_names.get(place_id)}: {e}")
            continue
        if isinstance(result, dict) and not result.get("error"):
            logger.info(f"Walked off in the prose: {names[npc_id]} -> {place_names.get(place_id)}")
            if npc_id in told:
                lines.append(f"(engine) Your story said {names[npc_id]} had gone (\"{told[npc_id][:60]}\"), so the "
                             f"engine moved them to {place_names.get(place_id)}. If they went somewhere else, write "
                             f"@npc-move {names[npc_id]} | <that place>; if they stayed, @npc-move {names[npc_id]} | "
                             f"{here_name}.")
                continue
            lines.append(f"(engine) {names[npc_id]} left for {place_names.get(place_id)} in your story, so the "
                         f"engine moved them there. If they stayed, write @npc-move {names[npc_id]} | {here_name}.")
    return lines


def _gated_hop_note(player_id: str, results: list[Any] | None, left_from: str | None, prose: str) -> str | None:
    """A held road this reply's @move crossed on what the player carries,
    in a reply that never showed it checked: one line the next reply reads.

    fleet10-bram T11: `@move Wolf's Hollow` from the Stable Yard went
    through the pass-held North Gatehouse road, and the prose went "past
    the paddock fence and into the lane… The quarry road climbs ahead" —
    the pass the opening had set up was never shown. The reply's own line
    comes first (travel_intent.gate_note, when the player named where they
    go); this is for the trip nobody named. None when the prose names the
    gate's place or the thing that opened it. Never raises."""
    from src.services import travel_intent

    moves = [r for r in results or [] if getattr(r, "ok", False) and getattr(r, "tool", "") == "move_player"
             and isinstance(r.result, dict) and r.result.get("success") and not r.result.get("already_here")]
    if not moves or not left_from:
        return None
    hops: list[dict[str, str]] = []
    try:
        with get_session() as db:
            player = db.get(Player, player_id)
            origin = left_from
            for r in moves:
                dest = db.query(Location).filter(Location.name == r.result.get("destination")).first()
                if dest is None:
                    continue
                hops += travel_intent.gated_hops(db, player, origin, dest.id)
                origin = dest.id
    except Exception as e:
        logger.debug(f"gated-hop note unavailable: {e}")
        return None
    lowered = (prose or "").lower().replace("’", "'")
    for hop in hops:
        place_words = re.findall(r"[a-z']+", hop["from"].lower())
        item_words = re.findall(r"[a-z']+", hop["item"].lower())
        said = [hop["from"].lower(), hop["item"].lower()] + [w for w in place_words[-1:] + item_words[-1:]
                                                               if len(w) >= 4]
        if any(re.search(rf"(?<![\w'-]){re.escape(w)}(?![\w-])", lowered) for w in said):
            continue
        return (f"On the way to {hop['to']} the player went through {hop['from']}, where the {hop['item']} they "
                "carry was checked and let them past; your reply did not show it. It happened — if the story "
                "looks back on that road, it was so.")
    return None


def _usage_snapshot(dm: Any) -> dict[str, int]:
    try:
        metrics = dm.agent.event_loop_metrics
        usage = dict(metrics.accumulated_usage or {})
        usage["_cycles"] = int(metrics.cycle_count or 0)
        return usage
    except Exception:
        return {}


def _log_turn_usage(dm: Any, before: dict[str, int], spoken_lines: int, dropped: list[str]) -> None:
    """One line per turn: model round-trips, tokens, cache hits — the cost of a turn."""
    after = _usage_snapshot(dm)
    if not after:
        return
    delta = {k: int(after.get(k) or 0) - int(before.get(k) or 0) for k in after}
    # Cache hits are real (OpenRouter reports ~all of the prompt cached) but
    # LiteLLM drops the cache fields from STREAMED usage, so they are not
    # logged here — a zero would be a lie
    logger.info(
        "Turn usage: %d model calls, in=%d out=%d, %d spoken line(s), %d bookkeeping dropped",
        delta.get("_cycles", 0), delta.get("inputTokens", 0), delta.get("outputTokens", 0),
        spoken_lines, len(dropped),
    )
    for sentence in dropped:
        logger.info(f"  dropped bookkeeping: {sentence[:160]!r}")


def _kindness_frame(player_id: str, outcome: Any, heard_now: str = "") -> str:
    """The toast for a gift that earned trust (`commands._gift_trust`):
    "Sorrel won't forget that." — the person as far as the player has heard
    their name, never a number. "" when the command earned none."""
    result = getattr(outcome, "result", None)
    gift = result.get("gift_trust") if getattr(outcome, "ok", False) and isinstance(result, dict) else None
    if not isinstance(gift, dict) or not gift.get("npc_name"):
        return ""
    try:
        names = dialogue_mod.NameGate.for_player(player_id, heard_now)
        who = names.name_for(gift.get("npc_id"), gift["npc_name"]) if names is not None else gift["npc_name"]
        person = names._persons.get(gift.get("npc_id")) if names is not None else None
        if who and who == getattr(person, "label", None):
            who = f"the {who}"
        event = {"type": "kindness", "npc_id": gift.get("npc_id"),
                 "text": f"{who[:1].upper()}{who[1:]} won't forget that."}
        return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
    except Exception as e:
        logger.debug(f"kindness toast skipped: {e}")
        return ""


def _drain_story_events(player_id: str, heard_now: str = "") -> list[str]:
    """SSE frames for everything the story engine queued out-of-band.

    Campaign beats/acts/resolutions and campaign-deferred cinematics fire in
    pure code (inside the world tick, or in `mark_beat_delivery` after the
    turn), so they have no tool result to ride on. Drained twice per turn:
    once after the context build, once after delivery marking — and after
    every command mid-reply, where `heard_now` is what the reply has said so
    far (no memory file holds it yet).
    """
    frames: list[str] = []
    try:
        # Beat titles, quest banners and objective toasts name people as far
        # as the player has heard them (fleet5: "✓ Ask Sorrel Tack what…")
        for event in dialogue_mod.player_facing(player_id, pop_pending_events(player_id), heard_now=heard_now):
            frames.append(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n")
    except Exception as e:
        logger.warning(f"Campaign event drain failed: {e}")
    try:
        for event in dialogue_mod.player_facing(player_id, objectives_mod.pop_pending_events(player_id),
                                                heard_now=heard_now):
            frames.append(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n")
    except Exception as e:
        logger.warning(f"Objective event drain failed: {e}")
    try:
        # A card's title is on screen too (a boss intro's is the boss's name)
        for cine in dialogue_mod.player_facing(player_id, drain_pending_cinematics(player_id), heard_now=heard_now):
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
            if stale_roster and not answers_combat_outcome(request.player_input):
                reissued = int(stale_roster.get("_reissued", 0))
                if reissued < MAX_ROSTER_REISSUES:
                    logger.warning("Re-issuing a combat roster the client never fought")
                    stale_roster["_reissued"] = reissued + 1
                    roster = {k: v for k, v in stale_roster.items() if not k.startswith("_")}
                    yield f"data: {json.dumps({'type': 'narration', 'content': 'The fight is not over.'})}\n\n"
                    yield f"data: {json.dumps(roster, ensure_ascii=False, default=str)}\n\n"
                    yield f"data: {json.dumps({'type': 'complete', 'tools': [], 'audit': []})}\n\n"
                    return
                # Never a soft-lock: a fight nobody answers (a crashed tab, a
                # bad result request) breaks off and the story goes on
                # (2026-10-04 autoplay: 13 turns of "The fight is not over.")
                _pending_combat.pop(request.player_id, None)
                names = ", ".join(e.get("name", "?") for e in stale_roster.get("enemies") or [])
                logger.warning(f"Combat roster abandoned after {reissued} re-issues: {names}")
                _pending_engine_notes.setdefault(request.player_id, []).append(
                    f"The fight with {names} broke off unresolved — nobody fell. Pick the scene up from "
                    "the scramble apart: the danger is still there, the player has a moment."
                )

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
            # Who knew the PLAYER's name BEFORE this turn — this turn's
            # dialogue writes rows before the audit runs, and the audit asks
            # whether someone used the player's name unprompted. A row is no
            # longer that knowledge (fleet6-bram T19: Piper, met, said "Bram",
            # which nobody had told her): its `knows_player_name` is (NULL, a
            # row from before the split, still counts as knowing)
            try:
                with get_session() as db:
                    met_before = {
                        row.npc_id for row in db.query(NPCRelationship).filter(
                            NPCRelationship.player_id == request.player_id,
                            dialogue_mod.knows_player_name_clause(),
                        ).all()
                    }
            except Exception:
                met_before = None

            # What the server knows that the DM must hear before the player's
            # words: last turn's audit, engine notes, how its commands landed
            extra_blocks: list[str] = []
            prior_warnings = _pending_audit_feedback.pop(request.player_id, None)
            if prior_warnings:
                extra_blocks.append(format_audit_feedback(prior_warnings))
            engine_notes = _pending_engine_notes.pop(request.player_id, None)
            if engine_notes:
                extra_blocks.append(
                    "[ENGINE NOTE — already happened, do not redo or repeat]\n- "
                    + "\n- ".join(engine_notes)
                )
            # The world strikes first (src/services/encounters.py): rolled
            # before the DM writes, so the fight is already on screen and the
            # DM is told to write its first moment. Never on an engine turn
            # ([OPENING], [COMBAT OUTCOME]) — those are answers, not turns.
            ambush_frames: list[str] = []
            # A reply is about to be written: a failed roll's hold on its step
            # runs for this reply and the next (src/services/stakes.py)
            stakes.begin_turn(request.player_id)
            if not (request.player_input or "").lstrip().startswith("["):
                with get_session() as db:
                    prow = db.get(Player, request.player_id)
                    here_id = prow.current_location_id if prow else None
                encounters.note_turn(request.player_id, here_id)
                # The player's own words can open a parley the roll respects
                # (fleet6-bram T10: "I'm here to talk, not fight")
                ambush = _engine_ambush(request.player_id, arrived=False,
                                        player_input=request.player_input or "")
                if ambush:
                    extra_blocks.append(encounters.directive(ambush["encounter"]))
                    if ambush.get("combat_start"):      # (a danger shown first starts no fight)
                        ambush_frames.append(
                            f"data: {json.dumps(ambush['combat_start'], ensure_ascii=False, default=str)}\n\n"
                        )
            # A payout waits out a danger: a risky act under way or a fight
            # still to fight holds [PAY THIS REPLY] (fleet9-tamsin T12;
            # src/services/settle.py)
            from src.services import settle as settle_mod

            settle_mod.note_turn(request.player_id, request.player_input or "",
                                 fight_pending=bool(ambush_frames) or request.player_id in _pending_combat)
            # An ambush reply is one sentence: the world tick spends no event
            # delivery attempt on it (engine inputs are read off the input)
            context = dm._build_context(
                request.player_input, canvas_notes=canvas_notes, addressed_npc_id=request.npc_id,
                command_ledger=_pending_command_ledger.pop(request.player_id, None),
                extra_blocks=extra_blocks, constrained=bool(ambush_frames),
            )
            brief = getattr(dm, "last_brief", None) or {}
            present_ids = list((brief.get("present") or {}).keys())
            # Who the player has seen where, today: a label borrowing the name
            # of someone seen elsewhere is no walk-up (dialogue.borrowed_speaker)
            dialogue_mod.note_seen(request.player_id, (state_before or {}).get("location_id"), present_ids)
            # What the player may see each person called this turn: the part
            # of a name they have HEARD said, else what they look like
            # (fleet4: labels and prose named people nobody had named)
            name_gate = dialogue_mod.NameGate.for_turn(
                request.player_id, request.player_input or "", present_ids=present_ids, context=context,
            )

            # Someone who hates the player and does not trust them will not
            # talk: code decides, and their one cold line is spoken by the
            # engine before the DM writes a word (the brief tells it so)
            refusal_speech: list[dict[str, Any]] = []
            refusal = brief.get("refusal")
            if refusal and refusal.get("line"):
                note = {"type": "speech", "npc_name": refusal.get("npc_name"),
                        "text": refusal["line"], "tone": "hostile", "action": None}
                try:
                    _enrich_speech_event(note)
                except Exception as e:
                    logger.warning(f"Speech TTS enrichment failed: {e}")
                note.setdefault("npc_id", refusal.get("npc_id"))
                name_gate.hear(refusal["line"])
                note["npc_name"] = _known_as(name_gate, note.get("npc_id"), note.get("npc_name"))
                refusal_speech.append(note)

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
            for note in refusal_speech:
                yield f"data: {json.dumps(note, ensure_ascii=False, default=str)}\n\n"
            for frame in ambush_frames:
                yield frame

            # Screenplay lines out of the DM's reply -> real speech events;
            # the model's bookkeeping sentences never reach the player
            def _accept_unquoted(speaker: str, tone: str | None) -> bool:
                return bool(tone) or dialogue_mod.is_known_speaker(speaker, present_ids)
            # A line in the player character's own voice is dropped whole
            markup = DialogueMarkupFilter(accept_unquoted=_accept_unquoted,
                                          player_name=getattr(dm, "_player_name", None))
            # ...and so is the prose remarking on a name the gate cut from a line
            meta = MetaSentenceFilter(player_name=getattr(dm, "_player_name", None),
                                      also_drop=name_gate.comments_on_cut_name)
            spoken: list[dict[str, Any]] = list(refusal_speech)
            usage_before = _usage_snapshot(dm)

            # A [COMBAT OUTCOME] reply narrates the aftermath; it may not start
            # another fight (fleet4-bram T7 chained the boss into the wolves')
            runner = CommandRunner(request.player_id, present_ids=present_ids,
                                   after_combat=answers_combat_outcome(request.player_input))
            runner.journal_mask = name_gate.mask
            # A take or a search is the player's to ask for (fleet7-tamsin T11)
            runner.player_input = request.player_input or ""
            # ...and a draught they drank from the quick slot is not drunk again
            runner.code_actions = set(pending_actions.tool_equivalents(canvas_actions))
            runner.canvas_used = [str(a.get("summary") or "") for a in canvas_actions if a.get("kind") == "consume"]

            def _command_frames(line: str) -> list[str]:
                """Apply one `@command` and stream what it changed: the engine
                events its result carries (quest banners, combat, deaths,
                cinematics, growth), a fresh state snapshot, objective ticks."""
                nonlocal last_state_json
                frames: list[str] = []
                outcome = runner.run(line)
                logger.info(f"Command {'ok ' if outcome.ok else 'FAILED'} {line.strip()[:140]}"
                            + (f" -> {outcome.message}" if outcome.message else ""))
                if outcome.ok and outcome.verb == "interact":
                    # What the Thing showed is read now, before the prose that
                    # quotes it is masked (fleet7-bram T18: "— Capt. B. Coldiron."
                    # went out as "— Capt. B. The Watch Captain.")
                    for read in dialogue_mod.read_texts(outcome.result):
                        name_gate.hear(read)
                if outcome.result:
                    try:
                        tool_tracker._parse_tool_output_for_events(
                            json.dumps(outcome.result, ensure_ascii=False, default=str)
                        )
                    except Exception as e:
                        logger.debug(f"command event extraction failed: {e}")
                for notification in tool_tracker.drain_notifications():
                    if notification.get("type") in ("tool_update", "tool_summary"):
                        continue
                    if notification.get("type") == "speech":
                        try:
                            _enrich_speech_event(notification)
                        except Exception as e:
                            logger.warning(f"Speech TTS enrichment failed: {e}")
                    elif notification.get("type") == "combat_start":
                        _mask_roster_names(notification, request.player_id, name_gate)
                        _pending_combat[request.player_id] = dict(notification)
                        encounters.reset(request.player_id)  # any fight restarts the ambush cooldown
                    elif notification.get("type") in ("quest_activated", "quest_completed", "quest_failed",
                                                      "objective_done", "cinematic"):
                        # A boss intro's card title is the boss's name: the
                        # bar under it already said "Wolf-Mantled Man"
                        notification = dialogue_mod.player_facing(request.player_id, notification,
                                                                  heard_now=name_gate.said_this_turn())
                    frames.append(f"data: {json.dumps(notification, ensure_ascii=False, default=str)}\n\n")
                kindness = _kindness_frame(request.player_id, outcome, name_gate.said_this_turn())
                if kindness:
                    frames.append(kindness)
                if outcome.ok and outcome.tool == "move_player":
                    frames.extend(_arrival_scene_frames(request.player_id))
                if outcome.ok and outcome.tool in COMMAND_STATE_TOOLS:
                    try:
                        mid_json = json.dumps(build_player_state(request.player_id,
                                                                 heard_now=name_gate.said_this_turn()),
                                              ensure_ascii=False, default=str)
                        if mid_json != last_state_json:
                            last_state_json = mid_json
                            frames.append(f'data: {{"type": "state", "state": {mid_json}}}\n\n')
                    except Exception as e:
                        logger.debug(f"Mid-turn state snapshot failed: {e}")
                frames.extend(_drain_story_events(request.player_id, heard_now=name_gate.said_this_turn()))
                return frames

            def _frames_for(segments: list[tuple[str, Any]]) -> list[str]:
                """Ordered SSE frames for markup output: prose runs through the
                rest of the pipeline (junk, bookkeeping, re-quotes, repeats),
                speaker lines become resolved, voiced speech events, command
                lines are applied to the world and never shown."""
                frames: list[str] = []
                for segment in segments:
                    kind, value = segment[0], segment[1]
                    if kind == "text":
                        # Junk releases whole lines, so the name mask never
                        # sees half a name; it runs before the deduper and
                        # bookkeeping filters, which read the prose as shown
                        released = sentences.filter(deduper.feed(meta.feed(name_gate.feed(junk.feed(value)))))
                        if released:
                            turn_text_parts.append(released)
                            frames.append(f"data: {json.dumps({'type': 'token', 'data': released})}\n\n")
                        continue
                    if kind == "command":
                        frames.extend(_command_frames(value))
                        continue
                    # Where the player stood when this line was spoken: a
                    # reply can talk in the square and then @move — and the
                    # people THERE are who speaks (fleet6-wenna T13: the
                    # Keeper's lines after `@move The Drowned Lantern` went
                    # out with no npc_id, no portrait, filed nowhere). A line
                    # the roll-first hold kept carries where it was written
                    where = segment[2] if len(segment) > 2 else runner.current_location_id()
                    note = _speech_from_markup(value, _present_at(present_ids, where), request.player_id,
                                               names=name_gate, where=where,
                                               left_from=(state_before or {}).get("location_id"),
                                               before="".join(turn_text_parts))
                    introduced = note.pop("_introduces", None)
                    borrowed = note.pop("_borrowed", None)     # a name in use, for the audit (never shown)
                    try:
                        _enrich_speech_event(note)
                    except Exception as e:
                        logger.warning(f"Speech TTS enrichment failed: {e}")
                    deduper.register_speech(note.get("text"))
                    if value.get("text") != note.get("text"):
                        # The DM's re-quote carries the line as written, the
                        # player's name and all: it goes as a copy, too
                        deduper.register_speech(value.get("text"))
                    # ...and how much prose came before it: a line from the
                    # place left, said once the prose has the player at the
                    # new one, walked along (fleet9-tamsin T7, `_along_ids`)
                    entry = {**note, "_where": where, "_at": sum(len(p) for p in turn_text_parts)}
                    if introduced is not None:
                        entry["introduces"] = introduced     # the gate's verdict, for record_turn_dialogue
                    if borrowed:
                        entry["_borrowed"] = borrowed
                    spoken.append(entry)
                    # The DM's own label stays server-side: it can carry a
                    # name the player never heard ("Vex Marrow", shown "Vex")
                    shown = {k: v for k, v in note.items() if k != "label"}
                    frames.append(f"data: {json.dumps(shown, ensure_ascii=False, default=str)}\n\n")
                return frames

            def _drain_prose() -> list[str]:
                """Release everything the prose filters still hold — a tool
                call is about to run, or the reply ended. The junk filter
                holds a line until its newline and the deduper a sentence
                until its end: left in place across a roll_check, the text
                after the tool glued onto them ("tonight.Something shifts",
                every glued sentence in four fleets was on a roll turn)."""
                held = meta.feed(name_gate.feed(junk.flush()) + name_gate.flush()) + meta.flush()
                released = sentences.filter(deduper.feed(held) + deduper.flush())
                if not released.strip():
                    return []
                turn_text_parts.append(released)
                return [f"data: {json.dumps({'type': 'token', 'data': released})}\n\n"]

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
            # At the reply's first roll_check: had it already written prose
            # or a speaker line? (None = no roll this turn)
            roll_seen: bool | None = None
            # What the dice said this turn: a partial or a failure must cost
            # something (turn_audit.partial_without_cost), and a quiet streak
            # ends on one (src/services/complications.py)
            roll_outcomes: list[str] = []
            # ...the runner reads the same list: a gate barred for the night
            # opens to a won roll, never to `| through` alone (fleet8-tamsin T13)
            runner.roll_outcomes = roll_outcomes
            # ...and what each roll was FOR (roll_check's own reason, read off
            # the stream): a failure binds the step it attempted, an unpaid
            # one is charged next reply, a roll nobody asked for is flagged
            roll_calls: dict[str, dict[str, Any]] = {}
            rolls_paired: set[str] = set()
            rolls: list[dict[str, Any]] = []
            # ...shared too: a barred gate opens to a roll FOR it, not a won
            # word with someone else (commands._won_the_night)
            runner.rolls = rolls
            # The dice before the prose, held to it (src/services/roll_intent.py):
            # when the context ordered roll_check FIRST, the reply's text and
            # speaker lines wait (commands still land as they come) until the
            # roll — then what was written before it is dropped unread — or
            # the reply ends, or HOLD_CHARS of it pile up. fleet8-bram T12/T19
            # showed two contradictory reaction paragraphs, one each side of
            # the roll. Ordinary turns never enter the hold
            from src.services import roll_intent

            roll_hold = roll_intent.ROLL_FIRST_MARK in context
            held: list[tuple[str, Any, Any]] = []
            held_chars = 0
            dropped_before_roll = 0
            kept_event = ""             # a MUST-DELIVER sentence the hold keeps (intents.must_deliver_sentences)

            def _hold(segments: list[tuple[str, Any]]) -> list[tuple]:
                """What passes now: commands, and — once the hold lets go at
                its cap — everything it kept, in order."""
                nonlocal roll_hold, held_chars
                out: list[tuple] = []
                for kind, value in segments:
                    if not roll_hold or kind == "command":
                        out.append((kind, value))
                        continue
                    # a speaker line keeps where it was said (text needs no place)
                    held.append((kind, value, runner.current_location_id() if kind == "speech" else None))
                    held_chars += len(value if kind == "text" else str((value or {}).get("text") or ""))
                    if held_chars > roll_intent.HOLD_CHARS:
                        logger.info("Roll-first hold released at its cap: no roll_check came")
                        roll_hold = False
                        out.extend(held)
                        held.clear()
                return out

            # Use stream_async to get streaming responses
            async for response in dm.agent.stream_async(context):
                event_count += 1
                logger.debug(f"Stream event {event_count}: {type(response)} - keys: {response.keys() if isinstance(response, dict) else 'N/A'}")

                # Process through tool tracker
                tool_tracker.process_stream_payload(response)
                stakes.capture_roll_input(response, roll_calls)

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
                        _mask_roster_names(notification, request.player_id, name_gate)
                        _pending_combat[request.player_id] = dict(notification)
                        encounters.reset(request.player_id)  # any fight restarts the ambush cooldown
                    elif notification.get("type") == "check_result" and notification.get("outcome"):
                        roll_outcomes.append(str(notification["outcome"]))
                        call = stakes.take_roll_call(roll_calls, rolls_paired)
                        rolls.append({"outcome": str(notification["outcome"]),
                                      "skill": notification.get("skill") or call.get("skill"),
                                      "reason": call.get("reason") or "",
                                      "about_npc_id": call.get("about_npc_id")})
                        # Before the reply's own commands: a @step-done after
                        # a failed count is refused (fleet6-wenna T8 -> T9)
                        stakes.note_roll(request.player_id, rolls[-1])
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
                            mid_state = build_player_state(request.player_id,
                                                           heard_now=name_gate.said_this_turn())
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
                        segments = markup.feed_segments(fence.feed(token_data))
                        # Held, a chunk goes through one segment at a time:
                        # a @move in it lands before the speaker line after
                        # it is held, so the line keeps the place it was said
                        for frame in (_frames_for(segments) if not roll_hold else
                                      [f for segment in segments for f in _frames_for(_hold([segment]))]):
                            yield frame

                # A tool call is about to run: the prose written so far is a
                # finished thought — end its line so a speaker line that
                # follows the tool starts clean, and so the next model call's
                # prose does not glue onto this sentence
                if isinstance(response, dict) and "current_tool_use" in response:
                    tool_name = (response.get("current_tool_use") or {}).get("name")
                    segments = markup.flush_segments()
                    if roll_hold:
                        segments = _hold(segments)
                        if tool_name == "roll_check" and roll_hold:
                            # Written before the dice: it assumed an outcome.
                            # Nobody read it, so nobody reads it now
                            dropped_before_roll = sum(len(v if k == "text" else str((v or {}).get("text") or ""))
                                                      for k, v, _ in held if k in ("text", "speech"))
                            if dropped_before_roll:
                                logger.info(f"Roll-first hold: dropped {dropped_before_roll} chars written "
                                            "before roll_check")
                            # ...but a world event happening where the player
                            # stands assumes no dice: its sentence stays
                            # (fleet9-wenna T10: the Evening Howl was thrown
                            # away with the prose, then filed as unseen)
                            from src.services import intents as intents_mod
                            kept_event = intents_mod.must_deliver_sentences(
                                "".join(str(v) for k, v, _ in held if k == "text"))
                            held.clear()
                            roll_hold = False
                    for frame in _frames_for(segments):
                        yield frame
                    for frame in _drain_prose():
                        yield frame
                    if tool_name == "roll_check" and roll_seen is None:
                        # The dice land before the prose: anything already
                        # written assumed an outcome (fleet4-tamsin T8: the
                        # wolves "settle lower", then the partial result
                        # made one lunge)
                        roll_seen = any(p.strip() for p in turn_text_parts) or len(spoken) > len(refusal_speech)
                    if kept_event:
                        # After the roll_seen read: the engine kept it, the DM's
                        # habit is still flagged by dropped_before_roll
                        for frame in _frames_for([("text", kept_event + "\n\n")]) + _drain_prose():
                            yield frame
                        kept_event = ""
                    last = "".join(turn_text_parts)[-2:]
                    if last and not last.endswith("\n"):
                        turn_text_parts.append("\n\n")
                        yield f"data: {json.dumps({'type': 'token', 'data': chr(10) * 2})}\n\n"

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

            # Release any text still held by the filters, in pipeline order:
            # fence (a held backtick) -> markup (an unfinished last line,
            # maybe a speaker line) -> junk -> bookkeeping -> the deduper.
            # A roll-first hold that never saw its roll lets everything go
            segments = markup.feed_segments(fence.flush()) + markup.flush_segments()
            if roll_hold:
                segments = _hold(segments) + held
                held.clear()
                roll_hold = False
            for frame in _frames_for(segments):
                yield frame
            for frame in _drain_prose():
                yield frame

            # Tool calls the DM typed instead of making: execute the dialogue
            # ones now, stream their speech, and tell the DM next turn
            # The rescue path executed dialogue TOOLS the DM typed as text —
            # there are no dialogue tools any more (speaker lines replaced
            # them). A fence is hidden and flagged by the audit below; any
            # `@command` lines inside it still count (models like to put
            # "code" in fences), applied after the prose like the rest
            rescued: list[dict[str, Any]] = []
            for fenced in fence.fences:
                for line in str(fenced).splitlines():
                    if line.lstrip().startswith("@"):
                        for frame in _command_frames(line):
                            yield frame
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

            # A hostile voice talking with the player buys the talk a turn
            # before the lingering ambush roll (the engine's cold refusal
            # line is not a parley). Before the state below: its chips hold
            # "Fight the wolves" back while the talk is THIS reply's
            try:
                await asyncio.to_thread(
                    encounters.note_parley, request.player_id,
                    [n.get("npc_id") for n in spoken[len(refusal_speech):] if n.get("npc_id")],
                    # a choice put to the player facing a danger holds the
                    # next roll once (fleet7-bram T8 -> T9)
                    "".join(turn_text_parts),
                )
            except Exception as e:
                logger.warning(f"Parley check failed: {e}")
            # A beast the player won against, or one the prose sent off into
            # the dark, leaves the scene now — before the complication count
            # below could press it as unanswered (fleet9-wenna T14 -> T16)
            sent_off: list[str] = []
            try:
                sent_off = await asyncio.to_thread(
                    encounters.send_off, request.player_id, "".join(turn_text_parts),
                    fought=bool(ambush_frames) or request.player_id in _pending_combat
                    or answers_combat_outcome(request.player_input),
                )
            except Exception as e:
                logger.warning(f"Send-off check failed: {e}")

            # The last reply's roll went unpaid and so did this one: the
            # engine charges the price itself, before the HUD snapshot below
            # (fleet6-bram T7 -> T8; src/services/stakes.py)
            engine_costs: list[dict[str, Any]] = []
            try:
                if stakes.owed(request.player_id):
                    from src.web.turn_audit import partial_without_cost

                    # A tip the player typed pays no debt (fleet8-tamsin T9)
                    paid = partial_without_cost(runner.results, ["failure"],
                                                (state_before or {}).get("health"),
                                                player_input=request.player_input or "") is None
                    charged = await asyncio.to_thread(
                        stakes.settle_owed_cost, request.player_id, paid=paid,
                        fought=bool(ambush_frames) or answers_combat_outcome(request.player_input),
                        present_ids=present_ids,
                    )
                    if charged:
                        engine_costs.append(charged)
                        _pending_engine_notes.setdefault(request.player_id, []).append(charged["note"])
            except Exception as e:
                logger.warning(f"Unpaid roll settlement failed: {e}")

            # Post-turn state snapshot so the HUD updates without polling
            state_after = None
            try:
                # A name said this turn shows on the tracker now, not a turn late
                state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                state_event = {"type": "state", "state": state_after}
                yield f"data: {json.dumps(state_event, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Could not build state event: {e}")

            # Did the DM actually deliver the beat the engine gave it? Runs
            # BEFORE the audit so a delivered beat is never flagged, and may
            # itself close an act (queueing more events + a title card).
            turn_text = "".join(turn_text_parts)
            spoken_text = "\n".join(str(n.get("text") or "") for n in spoken)
            # The beat, event and audit checks match what the DM WROTE: the
            # gate showed "the Ash-Cloaked Man", but delivery keywords say
            # "Vex Marrow" (fleet4's beats), and read against the masked text
            # a delivered beat was nagged and left to stall. What the player
            # reads (journal quotes, bubbles) keeps the masked text.
            dm_turn_text = "\n".join([turn_text] + list(dict.fromkeys(w for w, _ in name_gate.masked)))
            dm_labels = list(dict.fromkeys(w for w, _ in name_gate.unintroduced))

            # What was said this turn goes into each speaker's memory (their
            # dossier reads it back next time) and ticks talk_to objectives
            try:
                # A quest taken in this reply ticks none of its talk_to steps:
                # the player's words came before the job (fleet6-tamsin T4)
                started_quests = _quests_started(runner.results)
                await asyncio.to_thread(
                    dialogue_mod.record_turn_dialogue, request.player_id,
                    request.player_input, spoken, request.npc_id, started_quests,
                    left_from=(state_before or {}).get("location_id"),
                )
                # What people told the character, and what answered a step the
                # reply ruled, goes in their journal (fleet10-wenna: two entries
                # in 21 clue-hunting turns; memory_files.journal_testimony)
                from src.services import memory_files as memory_files_mod

                await asyncio.to_thread(memory_files_mod.journal_testimony, request.player_id, spoken,
                                        runner.results, turn_text, name_gate.mask)
                # A secret told in plain dialogue (no @reveal) still counts
                await asyncio.to_thread(dialogue_mod.note_confessions, request.player_id, spoken)
                # Every name the player has heard — this turn, or before in
                # their journal or another's words — is kept where one file
                # read finds it, and the rows of those people say so: their
                # dossier, labels and prose stop being a stranger's
                # (fleet6-wenna: "Maera" said at T12/T14, "the Keeper" to T18)
                await asyncio.to_thread(dialogue_mod.remember_heard, request.player_id,
                                        name_gate.heard_this_turn() + name_gate.known_words())
                await asyncio.to_thread(dialogue_mod.note_names_known, request.player_id,
                                        name_gate.known_ids())
                # Who learned the PLAYER's name: said aloud in the scene by the
                # player, given in the prose, or by someone who knew it
                from src.web.turn_audit import player_name_said

                player_name = getattr(dm, "_player_name", None)
                if player_name and player_name_said(
                        spoken, player_name, met_before or set(), turn_text, request.player_input or ""):
                    await asyncio.to_thread(dialogue_mod.note_player_name_heard, request.player_id,
                                            spoken, request.npc_id,
                                            left_from=(state_before or {}).get("location_id"))
            except Exception as e:
                logger.warning(f"Dialogue memory write failed: {e}")

            # Authored work this reply pitched is remembered; a pitched job
            # play already worked and nobody started is started by the engine,
            # its done steps credited (fleet7-wenna T2-T7; job_offers.py). Its
            # banner and ticks ride the story-event drain below
            try:
                await asyncio.to_thread(job_offers.note_reply, request.player_id, spoken,
                                        context=context, started=_quests_started(runner.results))
                taken = await asyncio.to_thread(job_offers.settle, request.player_id)
                if taken:
                    _pending_engine_notes.setdefault(request.player_id, []).extend(j["note"] for j in taken)
                    state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                    yield f"data: {json.dumps({'type': 'state', 'state': state_after}, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Job offer bookkeeping failed: {e}")
            # A voice with no row, counted: heard in a second reply, the next
            # context binds a @new-npc (fleet9-wenna T3-T6, the Gate Watchman;
            # src/services/intents.py)
            from src.services import intents as intents_mod
            intents_mod.note_speakers(request.player_id, spoken[len(refusal_speech):],
                                      (state_after or {}).get("location_id"))

            try:
                speakers = [n.get("npc_name") for n in spoken if n.get("npc_name")] + dm_labels
                # `spoken` (ids, words): beats about a speaker that play
                # delivered, and whose words a truth said aloud was. Off the
                # loop: the truth and play checks read the world's whole
                # vocabulary (~40 ms on the fleet worlds)
                await asyncio.to_thread(
                    mark_beat_delivery, request.player_id, dm_turn_text + "\n" + spoken_text,
                    speakers=speakers, spoken=spoken,
                    # a failed roll holds a beat back; a hook needs the
                    # player's own words or their talk chip (fleet6-bram T14)
                    player_input=request.player_input or "", roll_outcomes=roll_outcomes,
                    addressed_npc_id=request.npc_id,
                )
                # Fired world events the reply told are done being re-shown
                # (prose and speech apart: a MUST-DELIVER event is told only
                # by the prose — someone mentioning it is not it happening).
                # The player's words count as having heard of it (fleet9-wenna
                # T11 "The howl echoes in my ears"): never chronicled unseen
                world_tick_mod.mark_event_delivery(dm_turn_text, speakers=speakers, spoken_text=spoken_text,
                                                   player_input=request.player_input or "")
            except Exception as e:
                logger.warning(f"Campaign delivery marking failed: {e}")
            # A turn that cost the player nothing is a quiet one; enough in a
            # row and the next reply gets a complication (fleet5-wenna)
            try:
                from src.services import complications
                from src.web.turn_audit import theft_shown

                quarry_before = encounters.quarry_ids(request.player_id)
                complications.note_turn(
                    request.player_id, player_input=request.player_input or "", commands=runner.results,
                    roll_outcomes=roll_outcomes, state_before=state_before, state_after=state_after,
                    ambushed=bool(ambush_frames), refused=bool(refusal_speech),
                    # Did the reply answer the open complication? (fleet6-tamsin
                    # T17-T20: the watcher "hasn't moved")
                    speaker_ids=[n.get("npc_id") for n in spoken if n.get("npc_id")],
                    location_id=(state_after or {}).get("location_id"),
                    # ...a theft only once the player SAW it (fleet9-bram T11)
                    cost_seen=theft_shown(turn_text, runner.results),
                )
                # A thief this reply put in sight is a fight chip NOW — the
                # snapshot above was built before the count marked them
                if encounters.quarry_ids(request.player_id) - quarry_before:
                    state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                    yield f"data: {json.dumps({'type': 'state', 'state': state_after}, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Quiet-streak count failed: {e}")
            for chunk in _drain_story_events(request.player_id):
                yield chunk

            # Lines the DM wrote as prose for the NPC in front of the player
            # are voiced anyway (voice + bubble; the prose already shows the
            # words, so the client adds no second chat line). The audit below
            # still flags the habit so the DM learns to use speak.
            try:
                for notification in _synthesized_speech(request, turn_text, deduper, names=name_gate):
                    yield f"data: {json.dumps(notification, ensure_ascii=False, default=str)}\n\n"
            except Exception as e:
                logger.warning(f"Synthesized speech failed: {e}")

            # Fiction/database consistency audit — logged and attached to
            # the complete event (self-playtests read it as a scoreboard)
            audit_warnings = []
            # What the engine moved after the reply because the story said so
            # (the player's own walk, people who went along, a beast sent off):
            # ledger lines
            engine_walks: list[str] = list(sent_off)
            try:
                # Objectives the engine ticked during THIS turn are code
                # actions too — the DM narrating them is not a fabrication
                code_actions = set(code_actions or ()) | objectives_mod.pop_audit_tools(request.player_id)
                # A command that landed is the same fact a tool call was
                command_tools = [
                    {"name": r.tool, "status": "success"} for r in runner.results if r.ok and r.tool
                ]
                # Where the turn started, the things whose contents the player
                # has not seen: used this reply (@interact) or on the canvas
                # since the last one, they are seen. The engine's resolved
                # label and id ride along — "@interact the scratches" opened
                # "Scratches on the hut wall", and its reveal is no leak
                try:
                    unseen_objects = unseen_scene_objects(
                        request.player_id, (state_before or {}).get("location_id"),
                        [" ".join(str(x) for x in (r.line, (r.result or {}).get("label"),
                                                   (r.result or {}).get("object_id")) if x)
                         for r in runner.results if r.ok and r.verb == "interact"]
                        + [str(a.get("summary") or "") for a in canvas_actions or []],
                    )
                    # ...and what this turn's MUST-DELIVER beat ordered told
                    # is told by order, not leaked (fleet8-tamsin T15: SING)
                    from src.web.turn_audit import beat_named_objects

                    ordered = beat_named_objects(
                        unseen_objects, ((campaign_tick or {}).get("fired_beat") or {}).get("directive"))
                    for obj in ordered:
                        mark_object_seen(request.player_id, (state_before or {}).get("location_id"), obj.get("id"))
                    unseen_objects = [o for o in unseen_objects if o not in ordered]
                except Exception as e:
                    logger.debug(f"scene objects for the audit unavailable: {e}")
                    unseen_objects = []
                # A stranger the player gave their name to in an earlier
                # exchange knows it (fleet6-wenna T14 -> T15), as does one who
                # overheard it before they ever spoke (no row to keep it on)
                player_name = getattr(dm, "_player_name", None)
                if met_before is not None and player_name:
                    from src.services import memory_files
                    met_before = set(met_before) | {
                        n["npc_id"] for n in spoken or []
                        if n.get("npc_id") and n["npc_id"] not in met_before
                        and (memory_files.player_named_themself(request.player_id, n["npc_id"], player_name)
                             or dialogue_mod.overheard_player_name(request.player_id, n["npc_id"]))
                    }
                audit_warnings = audit_turn(
                    "\n".join([dm_turn_text] + dm_labels), tool_tracker.snapshot() + command_tools,
                    state_before, state_after,
                    code_actions=code_actions, campaign=campaign_audit,
                    spoken_text=spoken_text,
                    spoken=spoken,
                    player_name=getattr(dm, "_player_name", None),
                    met_before=met_before,
                    player_input=request.player_input or "",
                    scene_objects=unseen_objects,
                    # a hand-over the filters dropped still happened in the
                    # DM's story (fleet9-bram T6: "The Captain presses the pass
                    # into Bram's hand", dropped, and no @give)
                    unshown=_unshown_for_audit(meta.dropped, getattr(dm, "_player_name", None)),
                )
                # Walking into where the last reply's @move just took them, and
                # a wound a fight or a roll already gave them, are no claims
                # (fleet10-wenna T13 -> a refused @move at T14; fleet10-bram T18)
                from src.web import turn_audit as turn_audit_mod

                hurt_now = bool(ambush_frames) or answers_combat_outcome(request.player_input) or any(
                    r.ok and r.verb in ("fight", "boss") for r in runner.results) or any(
                    o in ("partial", "failure", "critical_failure") for o in roll_outcomes)
                audit_warnings = turn_audit_mod.drop_inner_arrival(audit_warnings, request.player_id, dm_turn_text,
                                                                   state_before)
                audit_warnings = turn_audit_mod.drop_told_wound(audit_warnings, request.player_id, dm_turn_text,
                                                                (state_before or {}).get("health"), hurt_now=hurt_now)
                left_from = (state_before or {}).get("location_id")
                # Where the player said they go, the reply's binding line said
                # to go, and no @move was written: the engine walks them there
                # (fleet8-tamsin T11/T12), before the carried-on check below
                # reads the prose from where they now stand
                walk = _walk_as_said(request.player_id, context, runner, left_from, turn_text,
                                     roll_outcomes, fought=bool(ambush_frames) or answers_combat_outcome(
                                         request.player_input) or request.player_id in _pending_combat,
                                     spoken_text=spoken_text)
                if walk and walk.get("ledger"):
                    engine_walks.append(walk["ledger"])
                    audit_warnings = [w for w in audit_warnings if w.get("check") != "arrival_without_move"]
                    for frame in _arrival_scene_frames(request.player_id) + _drain_story_events(
                            request.player_id, heard_now=name_gate.said_this_turn()):
                        yield frame
                    try:
                        state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                        yield f"data: {json.dumps({'type': 'state', 'state': state_after}, ensure_ascii=False, default=str)}\n\n"
                    except Exception as e:
                        logger.warning(f"Could not build state event after the engine's walk: {e}")
                elif walk and walk.get("audit"):
                    audit_warnings.append(walk["audit"])
                # The player took what was held out to them, the reply's binding
                # line said @give, and nothing gave it: the engine does, through
                # the reply's own runner (fleet9-wenna T8, fleet9-bram T6/T19;
                # src/services/intents.py)
                from src.services import intents as intents_mod
                took = intents_mod.taken_fallback(request.player_id, context, runner.results, dm_turn_text,
                                                  roll_outcomes)
                if took and took.get("line"):
                    for frame in _command_frames(took["line"]):
                        yield frame
                    landed = runner.results[-1] if runner.results else None
                    if landed is not None and landed.ok:
                        landed.message = ""         # the engine's own line below says it once
                        engine_walks.append(took["ledger"])
                        audit_warnings = [w for w in audit_warnings if not _hand_over_answered(w)]
                        state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                    else:
                        logger.warning(f"Engine take fallback refused: {took['line']} -> "
                                       f"{landed.message if landed else '?'}")
                        # The engine's try, never a line the reply wrote: a refusal
                        # left in the results would arm [SETTLE THIS FIRST] and read
                        # as the DM's own FAILED line next turn (settle.note_reply)
                        if landed is not None and landed.line == took["line"].strip():
                            runner.results.pop()
                        audit_warnings.append({"check": "taken_not_given", "detail": (
                            f"The player took what was held out to them and your reply wrote no @give; the "
                            f"engine could not give it either ({took['line']}: "
                            f"{(landed.message if landed else '') or 'refused'}). It is not in their pack. In "
                            "your next reply write the right @give first, or show its holder keeping it.")})
                elif took and took.get("audit"):
                    audit_warnings.append(took["audit"])
                # ...and a job taken in this very reply whose first thing its
                # holder, standing here, put in the player's hands in the prose
                # (fleet10-tamsin T2: the pass pressed into her palm with
                # @quest-start, no @give — the take line is built before the
                # reply, when that step did not exist yet)
                started = None if took else intents_mod.started_take(request.player_id, runner.results,
                                                                     dm_turn_text)
                if started:
                    for frame in _command_frames(started["line"]):
                        yield frame
                    landed = runner.results[-1] if runner.results else None
                    if landed is not None and landed.ok:
                        landed.message = ""
                        engine_walks.append(started["ledger"])
                        audit_warnings = [w for w in audit_warnings if not _hand_over_answered(w)]
                        state_after = build_player_state(request.player_id, heard_now=name_gate.said_this_turn())
                    else:
                        logger.warning(f"Engine started-job take refused: {started['line']} -> "
                                       f"{landed.message if landed else '?'}")
                        if landed is not None and landed.line == started["line"].strip():
                            runner.results.pop()
                # ...and its holder saying it is theirs, by name, is a hand-over
                # the reply never wrote (fleet9-tamsin T3: "That's your pass")
                aloud = intents_mod.handed_aloud_warning(request.player_id, spoken, runner.results)
                if aloud:
                    audit_warnings.append(aloud)
                # The prose walked on past where the commands left the player
                carried = _carried_on_to(request.player_id, dm_turn_text, left_from=left_from)
                if carried and not carried["blocked"]:
                    from src.tools.world_write.player import move_player as move_player_tool

                    moved = move_player_tool(player_id=request.player_id, destination_id=carried["id"])
                    if moved.get("success"):
                        audit_warnings = [w for w in audit_warnings if w.get("check") != "arrival_without_move"]
                        audit_warnings.append({
                            "check": "story_carried_on",
                            "detail": (
                                f"Your story took the player on from {carried['from']} to {carried['name']}"
                                + (f" (it showed {', '.join(carried['shown'])})" if carried["shown"] else "")
                                + ", past where your commands left them, so the engine moved them there to "
                                "match. Next time end the reply's @move where the story ends: one @move per "
                                "place they walk into."
                            ),
                        })
                        try:
                            state_after = build_player_state(request.player_id,
                                                             heard_now=name_gate.said_this_turn())
                            yield f"data: {json.dumps({'type': 'state', 'state': state_after}, ensure_ascii=False, default=str)}\n\n"
                        except Exception as e:
                            logger.warning(f"Could not build state event after carrying on: {e}")
                    else:
                        carried = {**carried, "blocked": True,
                                   "requirements": list(moved.get("requirements") or carried["requirements"])}
                if carried and carried["blocked"]:
                    needs = "; ".join(carried["requirements"]) or "the way is barred"
                    audit_warnings.append({
                        "check": "arrived_past_the_way",
                        "detail": (
                            f"Your story put the player at {carried['name']}, but they are still at "
                            f"{carried['from']}: the way there is blocked ({needs}). Nothing they saw, took or "
                            f"said at {carried['name']} happened. Open your next reply where they really are; "
                            f"only if the fiction truly got them through, write @move {carried['name']} | through "
                            "first."
                        ),
                    })
                if not any(w.get("check") == "arrival_without_move" for w in audit_warnings):
                    brought = _bring_speakers_here(request.player_id, spoken, left_from=left_from)
                    if brought:
                        audit_warnings.append({
                            "check": "speaker_brought_here",
                            "detail": (
                                f"You voiced people at the player's side whom the world had elsewhere, "
                                f"so the engine moved them to match your story ({'; '.join(brought)}). "
                                "Next time write @npc-move <name> | <place> when someone walks up. If "
                                "they were never really there, @npc-move them back."
                            ),
                        })
                # A speaker label that borrowed the name of someone the engine
                # kept where they are (fleet10-wenna T14, the driver called
                # "Hesk"; _speech_from_markup): name the new person
                borrowed = turn_audit_mod.name_in_use_borrowed_warning(
                    [n["_borrowed"] for n in spoken if n.get("_borrowed")])
                if borrowed:
                    audit_warnings.append(borrowed)
                # ...and a made-up suspect where the story already has one
                # (fleet10-wenna T7: the "unreadable" drover; src/services/suspects.py)
                suspect = turn_audit_mod.invented_suspect_warning(
                    request.player_id, runner.results, dm_turn_text,
                    _present_at(present_ids, (state_after or {}).get("location_id")))
                if suspect:
                    audit_warnings.append(suspect)
                # Someone the prose walked along with the player went too
                # (fleet8-bram T2: the Captain walked him to the gatehouse and
                # spoke there, and stayed in the square in the database)
                engine_walks.extend(_walked_along(request.player_id, turn_text, left_from, spoken))
                absent = _absent_speakers(request.player_id, spoken, left_from=left_from)
                if absent:
                    audit_warnings.append({
                        "check": "speaker_not_here",
                        "detail": (
                            f"{'; '.join(absent)}. The database is reality. If your story took the "
                            "player to them, the reply needed @move <their place> FIRST; if they came "
                            "to the player, @npc-move <name> | <where the player is>. Write the right "
                            "one at the top of your next reply and open from where everyone really is."
                        ),
                    })
                # ...and someone it sent off to the next place went (fleet9-bram
                # T12: Sorrel "ducks back off toward the Stable Yard" and stood
                # in the square all evening) — after the speaker check, which
                # would read their leaving as a line said elsewhere
                # ...or told as already gone, after the fact (fleet10-wenna T17:
                # "where the Drover stood is empty"; left_but_present when the
                # engine has nowhere to send them)
                engine_walks.extend(_walked_off(request.player_id, turn_text, spoken, results=runner.results,
                                                player_input=request.player_input or "", audits=audit_warnings))
                # Each present person's recurring details, for the brief's
                # "already used — vary" line (src/services/voice_tics.py)
                voice_tics.note_reply(request.player_id, turn_text, spoken)
                # Who the player saw where, after every move the engine made
                # (dialogue.borrowed_speaker reads it next reply); and what this
                # reply did that the next one's audit reads back
                # (turn_audit.drop_inner_arrival / drop_told_wound)
                dialogue_mod.note_seen_reply(request.player_id, spoken)
                worse = turn_audit_mod._health_worse((state_after or {}).get("health"),
                                                     (state_before or {}).get("health"))
                turn_audit_mod.note_reply_moves(
                    request.player_id,
                    (state_after or {}).get("location_id")
                    if (state_after or {}).get("location_id") != left_from else None,
                    hurt_now or worse)
                # A held road the reply's @move crossed on a pass the prose
                # never showed checked (fleet10-bram T11): the next reply knows
                gate_note = _gated_hop_note(request.player_id, runner.results, left_from, dm_turn_text)
                if gate_note:
                    _pending_engine_notes.setdefault(request.player_id, []).append(gate_note)
                # A wound dealt by someone here with no @fight, and dice that
                # failed or half-succeeded for free (fleet5-bram T8, -tamsin T10);
                # healing told and never applied (fleet6-bram T16); a roll the
                # player never asked for (fleet6-bram T19, -wenna T10)
                from src.web.turn_audit import consequence_warnings
                consequences = await asyncio.to_thread(
                    consequence_warnings,
                    request.player_id, runner.results, roll_outcomes, dm_turn_text,
                    present_ids=present_ids, state_before=state_before,
                    fought=bool(ambush_frames) or answers_combat_outcome(request.player_input),
                    state_after=state_after, spoken_text=spoken_text, code_actions=code_actions,
                    player_input=request.player_input or "", rolls=rolls,
                )
                audit_warnings.extend(consequences)
                # A theft charged in the very reply that brought the thief
                # (fleet9-bram T11; src/services/complications.py)
                from src.services import complications as complications_mod

                audit_warnings.extend(complications_mod.pop_warnings(request.player_id))
                # A refused line narrated as if it worked, and an authored clue
                # carried in the prose but lying where it was put (fleet7-tamsin
                # T10-T14, fleet7-wenna T16): said once, precisely, in place of
                # the generic warnings that would ask for the refused line again
                from src.web import turn_audit as turn_audit_mod

                pack = [str(i.get("name") or "") for i in (state_after or {}).get("inventory") or []
                        if isinstance(i, dict)]
                unheld = await asyncio.to_thread(turn_audit_mod.unheld_authored_items, request.player_id)
                # "it" after a refusal (this reply's or the last) is the
                # refused thing (fleet8-wenna T14/T15/T20)
                from src.services import settle as settle_mod

                refused_now = settle_mod.refused_items(runner.results) + settle_mod.recent_refusals(request.player_id)
                not_held = turn_audit_mod.carried_not_held(dm_turn_text, pack, unheld, refused=refused_now)
                if not_held:
                    audit_warnings.append(not_held)
                # A room asked for and handed over in prose is @rest (fleet9-wenna T20)
                room = turn_audit_mod.room_without_rest(dm_turn_text, runner.results, request.player_input or "",
                                                        (state_before or {}).get("location_id"))
                if room:
                    # ...its coin is the bed fee @rest charges, never a @coin of its own
                    audit_warnings = [w for w in audit_warnings if w.get("check") != "money_without_command"] + [room]
                audit_warnings = turn_audit_mod.merge_refused(audit_warnings, turn_audit_mod.refused_but_narrated(
                    runner.results, dm_turn_text, here=(state_after or {}).get("location"), pack=pack))
                # A job's pay up front charged back to its giver (fleet8-wenna T4)
                audit_warnings.extend(await asyncio.to_thread(
                    turn_audit_mod.advance_bought_warnings, runner.results, runner.advances))
                # ...and an unpaid one is remembered: if the next reply writes
                # no price either, the engine charges it (src/services/stakes.py)
                if any(w.get("check") == "partial_without_cost" for w in consequences):
                    stakes.owe(request.player_id, rolls, present_ids=present_ids,
                               addressed_npc_id=request.npc_id)
                if meta.rewritten:
                    audit_warnings.append({
                        "check": "third_person",
                        "detail": (
                            f"Narration is second person, but {len(meta.rewritten)} sentence(s) called the "
                            f"player character by name (such as {meta.rewritten[0][:80]!r}); the engine turned "
                            "them into \"you\". Write \"you\" for the player character in every sentence of "
                            "the story — never their name, never he or she."
                        ),
                    })
                if meta.dropped:
                    audit_warnings.append({
                        "check": "bookkeeping_in_prose",
                        "detail": (
                            "Your reply contained planning or bookkeeping the player must never "
                            f"read ({len(meta.dropped)} sentence(s) such as {meta.dropped[0][:80]!r}). "
                            "The engine removed them. Everything you write outside a tool call is "
                            "shown to the player: write only the story."
                        ),
                    })
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
                # Off the loop: the unknown-person check reads every authored
                # row (40-120 ms on the fleet worlds)
                audit_warnings.extend(await asyncio.to_thread(
                    _name_and_voice_warnings,
                    request, name_gate, markup, runner, spoken[len(refusal_speech):], turn_text,
                ))
                if roll_seen:
                    audit_warnings.append({
                        "check": "prose_before_roll",
                        "detail": (
                            "You wrote prose or dialogue BEFORE calling roll_check, so the player read "
                            "an outcome the dice had not decided yet. When an action needs a roll, make "
                            "roll_check your FIRST move — write nothing before it — then narrate what the "
                            "result says."
                        ),
                    })
                elif dropped_before_roll:
                    # The hold kept it off the screen; the habit still costs a
                    # reply's worth of words thrown away
                    audit_warnings.append({
                        "check": "prose_before_roll",
                        "detail": (
                            "You wrote prose or dialogue BEFORE calling roll_check; the engine held it back "
                            "and threw it away unread, because it assumed an outcome the dice had not given. "
                            "When the context says to roll first, make roll_check your FIRST move — write "
                            "nothing before it — then narrate only what the result says."
                        ),
                    })
                for w in audit_warnings:
                    logger.warning(f"Turn audit [{w['check']}]: {w['detail']}")
                if audit_warnings:
                    _pending_audit_feedback[request.player_id] = audit_warnings
            except Exception as e:
                logger.warning(f"Turn audit failed: {e}")
            # What the next context settles first and pays (src/services/
            # settle.py): a refused proof the prose claimed, a payout made
            # away from its giver. A payout nobody saw also keeps its coin
            # from being paid again from anywhere (fleet8-wenna T17/T18)
            try:
                from src.services import settle as settle_mod
                from src.services.commands import note_reward_unnarrated

                settle_mod.note_reply(request.player_id, runner.results, audit_warnings,
                                      paid_apart=runner.paid_away)
                if any(w.get("check") == "reward_unnarrated" for w in audit_warnings):
                    note_reward_unnarrated(request.player_id, [
                        str((r.result or {}).get("title") or "") for r in runner.results
                        if r.ok and r.verb == "quest-done"])
            except Exception as e:
                logger.warning(f"Settle bookkeeping failed: {e}")

            _log_turn_usage(dm, usage_before, len(spoken), meta.dropped)
            ledger = runner.ledger()
            if ledger:
                _pending_command_ledger[request.player_id] = ledger
            if engine_walks:
                # The engine's own moves read as the reply's commands landing
                _pending_command_ledger.setdefault(request.player_id, []).extend(engine_walks)
                for line in engine_walks:
                    logger.info(f"Engine walk: {line}")
            logger.info(f"Commands this turn: {len(runner.results)} "
                        f"({sum(1 for r in runner.results if not r.ok)} failed)")

            # Final payload with tool summary
            final_payload = {
                "type": "complete",
                "tools": tool_tracker.snapshot(),
                "audit": audit_warnings,
                # What the reply's @commands did (playtests and ?debug=1 read
                # it; the player-facing client ignores it)
                "commands": [
                    {"line": r.line[:200], "ok": r.ok, "message": r.message} for r in runner.results
                ],
            }
            if engine_costs:
                # A price the engine charged for an unpaid roll (playtests read it)
                final_payload["engine_costs"] = [{"line": c["line"], "message": c.get("message") or ""}
                                                 for c in engine_costs]
            if engine_walks:
                # Moves the engine made after the reply (playtests read it)
                final_payload["engine_walks"] = list(engine_walks)
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
    """Get quests for a player — named as far as they have heard, and proof
    they have never held shown as proof (dialogue.unfound_steps)."""
    quests = get_active_quests()
    try:
        with get_session() as db:
            quests = dialogue_mod.hide_unfound(quests, dialogue_mod.unfound_steps(db, db.get(Player, player_id)))
    except Exception as e:
        logger.debug(f"unfound proof steps not hidden: {e}")
    return dialogue_mod.player_facing(player_id, quests)


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


def _synthesized_speech(request, turn_text: str, deduper: DialogueDeduper, names: Any = None) -> list[dict[str, Any]]:
    """Speech events for quoted NPC lines the DM wrote inline this turn.

    The speaker is the NPC the player addressed (canvas click), else the
    only NPC present; with nobody to attribute to, nothing is voiced. With
    the turn's `names` gate the bubble carries what the player calls them,
    never a name they have not heard.
    """
    speaker, speaker_id = "", None
    with get_session() as db:
        # The id first: the client's `npc_name` is what the canvas shows
        # ("Stable Hand" until heard) — no row's name — and the real name it
        # used to send went onto the bubble whether heard or not
        npc = db.get(NPC, request.npc_id) if getattr(request, "npc_id", None) else None
        if npc is not None:
            speaker, speaker_id = npc.name, npc.id
        else:
            speaker = (getattr(request, "npc_name", None) or "").strip()
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
                    speaker_id = present[0].id
    if not speaker:
        return []
    shown = _known_as(names, speaker_id, speaker) if names is not None and speaker_id else speaker

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
        if speaker_id:
            notification["npc_id"] = speaker_id
        try:
            _enrich_speech_event(notification)
        except Exception as e:
            logger.warning(f"Speech TTS enrichment failed: {e}")
        notification["npc_name"] = shown
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
    # A line cut down to "…" (only the player's name, asked after) has
    # nothing to voice
    if not npc_name or not re.search(r"\w", text):
        return

    npc_id = notification.get("npc_id")
    if not npc_id:
        with get_session() as db:
            npc = db.query(NPC).filter(NPC.name.ilike(npc_name)).first()
            npc_id = npc.id if npc else None

    voice_gen = get_voice_generator()
    if not npc_id:
        # An unnamed speaker ("Gate Guard") still gets a voice: a stable pool
        # voice chosen from the label — bubble-less, but heard
        if not voice_gen.available() or len(text) > settings.audio.max_tts_chars:
            return
        voice_id = voice_gen.ambient_voice(npc_name)
        if not voice_id:
            return
        audio_id = compute_audio_id(f"ambient:{npc_name.lower()}", text, tone)
        notification["audio_id"] = audio_id
        notification["audio_ready"] = bool(voice_gen.line_path(audio_id))
        if not notification["audio_ready"]:
            get_asset_manager().spawn_generation(
                f"voice:{audio_id}",
                lambda: voice_gen.generate_line_for_voice(voice_id, text, tone, audio_id),
            )
        return
    notification["npc_id"] = npc_id

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
        # ...and the people where the lead points, so arriving there is too
        try:
            with get_session() as db:
                lead_row = db.get(Player, player_id)
                lead = lead_mod.compute_lead(db, lead_row) if lead_row else None
            target = (lead or {}).get("target") or {}
            if target.get("location_id") and target.get("location_id") != location_id:
                asset_manager.prewarm_people_at(target["location_id"])
        except Exception as e:
            logger.debug(f"lead pre-warm skipped: {e}")

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

        # A click on someone puts this name in the sidebar and the input
        # ("Talk to Sorrel Tack..."), and a talk chip prefers it to its own
        # words ("Approach the stable hand"): named as far as heard (round 4)
        names = dialogue_mod.NameGate.for_player(player_id)
        hidden = _offstage(player_id)

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
                    "name": names.name_for(npc["id"], npc["name"]) if names is not None else npc["name"],
                    "x": npc["x"],
                    "y": npc["y"],
                    "scale": npc.get("scale", 1.0),
                    "status": npc.get("status", "alive"),
                    "sprite_url": _url(npc["sprite_path"]),
                    "tier": npc["tier"],
                    "companion": bool(npc.get("companion")),
                    "condition": npc.get("condition"),
                }
                for npc in assets["npcs"] if npc["id"] not in hidden
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
    # The party: who fought beside them, who went down (wounded, never killed here)
    ally_npc_ids: list[str] = []
    downed_allies: list[str] = []
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
    encounters.reset(request.player_id)     # the cooldown runs from the end of a fight

    init_db(get_active_db_path())
    changes = apply_combat_result(
        request.player_id,
        request.outcome,
        hits_taken=request.hits_taken,
        hearts_lost=request.hearts_lost,
        hearts_healed=request.hearts_healed,
        defeated_npc_ids=request.defeated_npc_ids,
        defeated_positions=request.defeated_positions,
        downed_allies=request.downed_allies,
        ally_npc_ids=request.ally_npc_ids,
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
        "objectives_done": dialogue_mod.player_facing(request.player_id,
                                                      objectives_mod.pop_pending_events(request.player_id)),
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
    # Walking into a lair can be the last thing you do quietly
    encounters.note_turn(request.player_id, request.destination_id)
    ambush = _engine_ambush(request.player_id, arrived=True)
    return {
        "success": True, **result,
        "state": build_player_state(request.player_id),
        "objectives_done": dialogue_mod.player_facing(request.player_id,
                                                      objectives_mod.pop_pending_events(request.player_id)),
        **({"ambush": ambush} if ambush else {}),
    }


def _mask_roster_names(notification: dict[str, Any], player_id: str, names: Any = None) -> None:
    """In place: a stranger fights under what they look like, not their name.

    The combat UI's health bars named the villain before anyone had (round-2
    critique: "the engine's combat UI leaks real names"). Beasts keep theirs.
    Mid-turn, the turn's `names` gate decides: the bar says what the speech
    bubbles say ("Vex" when only "Vex" was heard).
    """
    try:
        with get_session() as db:
            # Allies too: a companion's bar says "Sorrel", as their bubbles do
            for entry in [*(notification.get("enemies") or []), *(notification.get("allies") or []),
                          notification.get("boss") or {}]:
                npc_id = entry.get("npc_id") if isinstance(entry, dict) else None
                npc = db.get(NPC, npc_id) if npc_id else None
                if npc is not None and entry.get("name"):
                    entry["name"] = (names.known_as(npc) if names is not None
                                     else dialogue_mod.display_name(db, player_id, npc))
    except Exception as e:
        logger.debug(f"roster name masking skipped: {e}")


def _engine_ambush(player_id: str, *, arrived: bool, player_input: str = "") -> dict[str, Any] | None:
    """Roll the encounter director; on a hit, build the real roster and
    remember it as the fight the next turn must see answered."""
    encounter = (encounters.roll(player_id, arrived=arrived, player_input=player_input) if player_input
                 else encounters.roll(player_id, arrived=arrived))
    if not encounter:
        return None
    if encounter.get("shown_first"):
        # The player turned to go: the danger shows itself first and the
        # choice is theirs next reply — no fight on screen (fleet10-tamsin T18)
        return {"encounter": encounter, "combat_start": None}
    roster = build_combat_start(encounter["npc_ids"], player_id)
    if not isinstance(roster, dict) or roster.get("error"):
        logger.warning(f"ambush roster refused: {roster}")
        return None
    notification = {"type": "combat_start", "enemies": roster.get("enemies") or [],
                    "allies": roster.get("allies") or [], "boss": roster.get("boss"),
                    "ambush": True}
    _mask_roster_names(notification, player_id, dialogue_mod.NameGate.for_player(player_id))
    _pending_combat[player_id] = dict(notification)
    pending_actions.record_action(
        player_id, "combat", f"ambush: {', '.join(encounter['names'])} attacked at {encounter['where']}",
        tools=("start_combat",),
    )
    # The toast is the player's: the roster's shown names, never the real
    # ones ("Vex Marrow come out of nowhere" before anyone had said Vex)
    shown = [str(e["name"]) for e in notification["enemies"] if isinstance(e, dict) and e.get("name")]
    return {"combat_start": notification, "encounter": encounter,
            "line": f"{', '.join(shown) or 'They'} come out of nowhere."}


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
        if str(entry.get("type") or "") == "quest_item":
            continue        # a key or clue they carry is handed over in the story, never sold (the Gate Pass)
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
    # The panel reads "<name>'s counter": "Trade with the hawker" opened on
    # "Hesk Bramble's counter" before anyone had said it (round 4 review)
    names = dialogue_mod.NameGate.for_player(player.id)
    return {
        "npc_id": npc.id,
        "npc_name": names.name_for(npc.id, npc.name) if names is not None else npc.name,
        "standing": faction_band,
        "disposition": standing.disposition_band(relationship),
        # "Hesk Bramble likes them", and the shelf's own words, as far as heard
        "reason": names.shown(reason) if names is not None else reason,
        "multiplier": mul,
        "currency": player.currency or 0,
        "npc_currency": standing.npc_purse(npc),
        "stock": dialogue_mod.player_facing_items(stock, names),
        "sellable": dialogue_mod.player_facing_items(sellable, names),
    }


def _shop_guard(db, player_id: str, npc_id: str, *, need_stock: bool):
    """(player, npc, error_response). The refusals a counter can give."""
    player = db.query(Player).filter(Player.id == player_id).first()
    if not player:
        return None, None, JSONResponse({"error": "Player not found", "reason": "missing"}, status_code=404)
    npc = db.get(NPC, npc_id)
    if not npc or npc.status != "alive":
        return None, None, JSONResponse({"error": "No one is trading here", "reason": "missing"}, status_code=404)
    def who() -> str:              # a refusal toast names them as far as heard
        names = dialogue_mod.NameGate.for_player(player_id)
        return names.name_for(npc.id, npc.name) if names is not None else npc.name

    if npc.current_location_id != player.current_location_id:
        return None, None, JSONResponse({"error": f"{who()} is not here", "reason": "not_here"}, status_code=409)
    if need_stock and not (npc.inventory_notable or []) and standing.npc_purse(npc) is None:
        return None, None, JSONResponse({"error": f"{who()} has nothing to trade", "reason": "no_shop"}, status_code=404)
    refusal = standing.conversation_refusal(db, player, npc)
    if refusal:
        return None, None, JSONResponse(
            {"error": f"{who()} will not trade with you", "reason": "standing", "line": refusal["line"]},
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
        elif any(isinstance(e, dict) and progression.item_key(e) == request.item_id
                 and str(e.get("type") or "") == "quest_item" for e in (npc.inventory_notable or [])):
            # ...nor is what they carry for the story (the Captain's Gate Pass)
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
    note = pending_actions.shop_note("sold" if selling else "bought", str(item_name), quantity, npc_name,
                                     result["cost"])
    if result.get("auto_equipped"):
        # A bought upgrade goes on at the counter (progression.auto_equip):
        # the next reply's prose puts it in their hand, not in the bag
        with get_session() as db:
            bought = next((e for e in (db.get(Player, request.player_id).inventory or [])
                           if isinstance(e, dict) and progression.item_key(e) == request.item_id), {})
        slot = (progression.mechanize_item(bought) or {}).get("slot") or "weapon"
        note += " " + pending_actions.equip_note(result["auto_equipped"], slot, automatic=True)
    pending_actions.record_action(request.player_id, "shop", note, tools=("transfer_item", "adjust_currency"))
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
        # Seen where it stands, whatever room the next typed turn starts in
        # (read the notice, walk into the inn, then type)
        mark_object_seen(request.player_id, request.location_id, request.object_id)
        pending_actions.record_action(
            request.player_id, "loot" if result["kind"] == "body" else result["kind"],
            _interact_summary(result), tools=tuple(tools),
        )
        # A name it showed is a name learned, now: the next reply's brief and
        # gate, and the HUD below, know it (fleet7-bram T18: a signed notice)
        await asyncio.to_thread(dialogue_mod.remember_read, request.player_id, dialogue_mod.read_texts(result))
        # What it read and held goes in their journal (round 4: 0-1 entries a session)
        from src.services import memory_files
        await asyncio.to_thread(memory_files.journal_interaction, request.player_id, result,
                                request.location_id)
    if isinstance(result.get("result"), dict):
        # What it held, as the player may read it (fleet8-tamsin T15: the
        # whistle's text named "Captain Coldiron", never said aloud). A
        # read's own names were heard just above, so the words stand
        result = {**result, "result": dialogue_mod.player_facing(request.player_id, result["result"])}
    return {
        **result,
        "state": build_player_state(request.player_id),
        "objectives_done": dialogue_mod.player_facing(request.player_id,
                                                      objectives_mod.pop_pending_events(request.player_id)),
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
    `?spoilers=1` returns them and is a documented author/debug flag.
    Without it, people are named as far as the player has heard them (the
    act premises named "Vane" and "Coldiron" before anyone had, fleet5)."""
    init_db(get_active_db_path())
    view = campaign_public_view(player_id, spoilers=bool(spoilers))
    return view if spoilers else dialogue_mod.player_facing(player_id, view)


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
            # A visited place first (a world someone has played), else the
            # most populated place — never an empty parent node
            loc = (db.query(Location).filter(Location.visited == True).first()  # noqa: E712
                   or pick_spawn_location(db))
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

    # A knife in the hand and a draught on Q (src/services/starting_kit.py)
    await asyncio.to_thread(grant_starting_kit, player_id)

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
    from src.services import memory_files

    memory_files.forget_player(player_id)
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
            # Start where the people are. The old pick — the first
            # SETTLEMENT/TOWN-typed row — put a fresh Emberfall character in
            # the empty town node above the square (playtest 2026-10-04), and
            # the DM tore the world up trying to populate it.
            location = pick_spawn_location(db)

            # Create placeholder player (same purse as POST /api/world/players)
            player = Player(
                name="Unnamed",
                description="A mysterious figure awaiting their story.",
                current_location_id=location.id if location else None,
                currency=25,
            )
            db.add(player)
            db.flush()
            placeholder_id = player.id
            logger.info(f"Created placeholder player: {placeholder_id}")

        db.commit()

    if not players:
        grant_starting_kit(placeholder_id)

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

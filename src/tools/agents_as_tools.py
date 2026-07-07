"""Agent delegation tools for the DM orchestrator.

These tools allow the DM to delegate to specialized sub-agents.
"""

import os
import tempfile

from strands import tool
from strands.session.file_session_manager import FileSessionManager
from strands_semantic_memory.message_utils import extract_text_content


def get_recent_dm_context(player_id: str, num_messages: int = 20, stop_at_npc_id: str | None = None) -> str:
    """Get recent conversation context from the DM agent's session.

    Walks backward through the DM's conversation history, collecting text
    messages. If stop_at_npc_id is provided, stops when it encounters a
    prompt_npc_agent call for that same NPC (since the NPC already has its
    own session history for that earlier conversation).

    Args:
        player_id: The player's ID (used as session_id for DM).
        num_messages: Max number of recent text messages to retrieve.
        stop_at_npc_id: If set, stop collecting when we hit a prior
            prompt_npc_agent call for this NPC ID.

    Returns:
        Formatted string with recent conversation context.
    """
    from pathlib import Path

    try:
        storage_dir = os.path.join(tempfile.gettempdir(), "strands/sessions")

        session_path = Path(storage_dir) / f"session_{player_id}" / "agents" / "agent_default" / "messages"
        if not session_path.exists():
            return ""

        session_manager = FileSessionManager(session_id=player_id, storage_dir=storage_dir)

        all_messages = session_manager.list_messages(
            session_id=player_id,
            agent_id="default",
        )

        if not all_messages:
            return ""

        # Walk backward, collecting text messages and checking for NPC call boundary
        collected = []
        for session_msg in reversed(all_messages):
            if len(collected) >= num_messages:
                break

            message = session_msg.to_message()
            content = message.get("content", [])

            # Check if this message contains a prompt_npc_agent call for the same NPC
            if stop_at_npc_id:
                for block in content:
                    if isinstance(block, dict) and "toolUse" in block:
                        tool_use = block["toolUse"]
                        if (tool_use.get("name") == "prompt_npc_agent"
                                and tool_use.get("input", {}).get("npc_id") == stop_at_npc_id):
                            # Hit a prior conversation with this NPC — stop here
                            break
                else:
                    # No break in inner loop — check text
                    text = extract_text_content(message)
                    if text.strip():
                        role = message.get("role", "unknown")
                        label = "Player" if role == "user" else "Narrator"
                        collected.append(f"{label}: {text.strip()}")
                    continue
                # Inner loop broke — we hit the NPC boundary
                break

            # No NPC boundary check — just collect text
            text = extract_text_content(message)
            if text.strip():
                role = message.get("role", "unknown")
                label = "Player" if role == "user" else "Narrator"
                collected.append(f"{label}: {text.strip()}")

        if not collected:
            return ""

        # Reverse back to chronological order
        collected.reverse()
        return "Recent conversation:\n" + "\n".join(collected)

    except Exception:
        # If we can't read the session, return empty context
        return ""


def _record_npc_exchange(
    npc_id: str,
    player_id: str,
    player_input: str,
    npc_response: str,
    is_first_interaction: bool = False,
    conversation_ended: bool = False,
) -> None:
    """Persist one dialogue exchange to the NPC relationship (DB-backed memory).

    This is the canonical memory write path: the session file is a cache,
    but the relationship row is what survives restarts, session compaction,
    and corrupted-session self-heals. Never raises — memory writes must not
    break dialogue.
    """
    try:
        from src.config import load_settings
        from src.models import NPCRelationship, WorldClock, get_session

        settings = load_settings()
        keep = getattr(settings.game, "recent_messages_limit", 10) * 2  # pairs
        compress_at = getattr(settings.game, "summary_trigger_threshold", 20)

        with get_session() as session:
            rel = session.query(NPCRelationship).filter(
                NPCRelationship.npc_id == npc_id,
                NPCRelationship.player_id == player_id,
            ).first()
            if not rel:
                rel = NPCRelationship(npc_id=npc_id, player_id=player_id)
                session.add(rel)

            clock = session.query(WorldClock).first()
            day = clock.day if clock else 1

            messages = list(rel.recent_messages or [])
            if player_input:
                messages.append({"role": "player", "content": player_input[:500], "day": day})
            if npc_response:
                messages.append({"role": "npc", "content": npc_response[:500], "day": day})

            # Fold overflow into the summary so old exchanges compress
            # instead of vanishing
            if len(messages) > compress_at:
                overflow = messages[:-keep] if keep < len(messages) else []
                messages = messages[-keep:]
                if overflow:
                    lines = [
                        f"(Day {m.get('day', '?')}) {m.get('role', '?')}: {m.get('content', '')[:120]}"
                        for m in overflow
                    ]
                    summary = (rel.summary or "") + "\n" + "\n".join(lines)
                    rel.summary = summary[-3000:]  # keep the tail

            rel.recent_messages = messages
            rel.last_interaction_day = day

            key_moments = list(rel.key_moments or [])
            if is_first_interaction and not key_moments:
                key_moments.append(f"First met on Day {day}")
                rel.key_moments = key_moments
            elif conversation_ended:
                key_moments.append(f"Talked on Day {day}: \"{(player_input or '')[:80]}\"")
                rel.key_moments = key_moments[-15:]

            session.commit()
    except Exception:
        # Memory write failures must never surface as dialogue errors
        import logging
        logging.getLogger(__name__).warning(
            f"Failed to record NPC exchange for {npc_id}", exc_info=True
        )


def _build_world_snapshot(player_id: str) -> str:
    """Build a snapshot of current world state for sub-agent context bridging.

    Gives sub-agents awareness of the current scene so they can act
    coherently without needing to query everything themselves.

    Args:
        player_id: The player's ID.

    Returns:
        Formatted string with current world state summary.
    """
    from src.tools.world_read import (
        get_current_location,
        get_available_destinations,
        get_world_clock,
        get_recent_events,
    )

    parts = []

    try:
        location = get_current_location(player_id)
        loc_name = location.get("name", "Unknown")
        loc_type = location.get("type", "unknown")
        loc_id = location.get("id", "")
        parts.append(f"Current location: {loc_name} ({loc_type}) [id={loc_id}]")

        # NPCs at current location
        npcs_here = location.get("npcs_present", [])
        if npcs_here:
            npc_list = ", ".join(f"{n['name']} (id={n['id']})" for n in npcs_here)
            parts.append(f"NPCs here: {npc_list}")
        else:
            parts.append("NPCs here: None")

        # Available destinations
        if loc_id:
            destinations = get_available_destinations(loc_id)
            if destinations:
                dest_list = ", ".join(
                    f"{d.get('name', '?')} (id={d.get('id', '?')})"
                    for d in destinations
                )
                parts.append(f"Connected locations: {dest_list}")

        # Time
        clock = get_world_clock()
        parts.append(f"Time: Day {clock.get('day', 1)}, {clock.get('hour', 8)}:00 ({clock.get('time_of_day', 'day')})")

        # Recent events (last 3 days, brief)
        events = get_recent_events(days_back=3, player_visible_only=True)
        if events:
            event_lines = [f"  - {e.get('name', '?')}: {e.get('description', '')[:80]}" for e in events[:5]]
            parts.append("Recent events:\n" + "\n".join(event_lines))

    except Exception:
        parts.append("(Could not load full world snapshot)")

    return "\n".join(parts)


@tool
def prompt_creator_agent(player_id: str, instruction: str) -> dict[str, str]:
    """Delegate world creation tasks to the Creator Agent.

    Use this when you need to:
    - Generate new locations when players explore ungenerated areas
    - Create NPCs on-demand (any tier: major, minor, ambient)
    - Add new factions or update faction relationships
    - Create quests dynamically during gameplay
    - Expand the world as the player explores

    Args:
        player_id: The player's ID in the database.
        instruction: Detailed instruction for what to create/update. Be specific about:
            - What type of content to create (location, NPC, faction, quest)
            - How it should connect to existing content
            - Any specific requirements (tier, faction affiliation, etc.)

    Returns:
        Dictionary with the agent's response describing what was created.
    """
    from src.agents.creation_agent import CREATORAgent
    from src.tools.world_read import get_all_npcs, get_all_locations

    # Pre-fetch existing entities so Creator knows what NOT to duplicate
    existing_npcs = []
    existing_locations = []
    try:
        existing_npcs = get_all_npcs()
        existing_locations = get_all_locations()
    except Exception:
        pass

    npc_summary = ", ".join(
        f"{n['name']} (id={n['id']}, loc={n.get('current_location_id', '?')})"
        for n in existing_npcs
    ) if existing_npcs else "None"

    loc_summary = ", ".join(
        f"{loc['name']} (id={loc['id']})"
        for loc in existing_locations
    ) if existing_locations else "None"

    # Get recent DM context so Creator knows what just happened narratively
    dm_context = get_recent_dm_context(player_id, num_messages=6)
    world_snapshot = _build_world_snapshot(player_id)

    # Build enriched instruction with existing entities front-loaded
    enriched = f"""## EXISTING ENTITIES — DO NOT DUPLICATE THESE
**Existing NPCs:** {npc_summary}
**Existing Locations:** {loc_summary}

## CURRENT SCENE
{world_snapshot}

## RECENT NARRATIVE
{dm_context if dm_context else "(No recent context)"}

## CREATION REQUEST
{instruction}"""

    try:
        agent = CREATORAgent(player_id)
        result = agent.process_input(enriched)
    except Exception as e:
        return {
            "text_response": "The creation could not be completed due to an internal error. Improvise narratively without new entities for now.",
            "error": str(e),
        }

    return {"text_response": str(result)}


@tool
def prompt_npc_agent(player_id: str, npc_id: str, player_input: str, is_first_interaction: bool = False, context: str = "") -> dict[str, str]:
    """Get a response from an NPC agent for player interaction.

    Use this when the player wants to have a conversation with a named NPC.
    The NPC agent maintains conversation history across multiple calls.

    Args:
        player_id: The player's ID in the database.
        npc_id: The NPC's ID.
        player_input: What the player said or did toward the NPC.
        is_first_interaction: True if this is the first time the player is interacting with this NPC in this session.
        context: Additional context about the interaction. It is encouraged to provide context on the first interaction. The NPC should sometimes know the context (Is the NPC expecting the player? Is the player a stranger?).

    Returns:
        Dictionary with the NPC's response text.
    """
    # Import inside function to avoid circular imports
    from src.repositories.unit_of_work import unit_of_work
    from src.agents.npc_agent import NPCAgent
    from src.tools.world_read.player import get_player

    # Validate NPC exists before creating agent (fixes "nothing to say" bug)
    with unit_of_work() as uow:
        npc_result = uow.npcs.validate_for_conversation(npc_id)
        if not npc_result.success:
            return {"text_response": npc_result.error, "error": npc_result.error_code}

        npc_data = uow.npcs.to_dict(npc_result.data)

        # Get relationship data
        rel_result = uow.npcs.get_with_relationship(npc_id, player_id)
        _, relationship = rel_result.data if rel_result.success else (None, None)

        from src.config import load_settings
        recent_limit = getattr(load_settings().game, "recent_messages_limit", 10)

        relationship_dict = {
            "summary": relationship.summary if relationship else "You have not met this person before.",
            "trust_level": relationship.trust_level if relationship else 50,
            "current_disposition": relationship.current_disposition if relationship else "neutral",
            "key_moments": relationship.key_moments if relationship else [],
            "recent_messages": (relationship.recent_messages or [])[-recent_limit * 2:] if relationship else [],
            "revealed_secrets": relationship.revealed_secrets if relationship else [],
        }

    # Auto-fetch recent DM context — grab up to 20 messages but stop if we
    # hit a prior conversation with THIS same NPC (it has its own memory for that)
    dm_context = get_recent_dm_context(player_id, num_messages=20, stop_at_npc_id=npc_id)
    world_snapshot = _build_world_snapshot(player_id)

    # Narrative context = DM conversation + explicit DM context (for user messages)
    narrative_parts = []
    if dm_context:
        narrative_parts.append(dm_context)
    if context:
        narrative_parts.append(f"Additional context: {context}")
    narrative_context = "\n\n".join(narrative_parts)

    # Create NPC agent with validated data
    conversation_ended = False
    try:
        agent = NPCAgent(player_id, npc_id)
        # Scene context goes into the system prompt so NPC always knows where it is
        agent._scene_context = world_snapshot

        if is_first_interaction:
            # First interaction - start a new conversation with greeting
            response = agent.start_conversation(npc=npc_data, relationship=relationship_dict, context=narrative_context)
            conversation_ended = "[END_CONVERSATION]" in str(response)
            response = str(response).replace("[END_CONVERSATION]", "").strip()
        else:
            # Continuing conversation - pass the player's actual words
            # Still need to initialize the agent with NPC data before responding
            agent.npc = npc_data
            agent.relationship = relationship_dict
            player_data = get_player(player_id)
            agent._player_name = player_data.get("name", "Unknown") if player_data else "Unknown"
            agent._agent = agent._create_agent()

            result = agent.respond(player_input, context=narrative_context)
            response = result.get("response", "...")
            conversation_ended = result.get("conversation_ended", False)
    except Exception as e:
        return {
            "text_response": f"{npc_data.get('name', 'The NPC')} seems distracted and doesn't respond right now. (internal error — narrate around it)",
            "error": str(e),
        }

    # Canonical memory write: this is what makes the NPC remember across
    # sessions regardless of what happens to the session file
    _record_npc_exchange(
        npc_id=npc_id,
        player_id=player_id,
        player_input=player_input,
        npc_response=str(response),
        is_first_interaction=is_first_interaction,
        conversation_ended=conversation_ended,
    )

    return {
        "text_response": str(response),
        "conversation_ended": conversation_ended,
        "note": "This dialogue was ALREADY shown to the player. Do not repeat or paraphrase it — react to it or move the scene forward.",
    }


@tool
def prompt_research_agent(session_id: str, query: str) -> dict[str, str]:
    """Delegate research tasks to the Research Agent for gathering reference material.

    Use this when you need to:
    - Research real-world history, mythology, or cultures for inspiration
    - Look up details about existing fictional universes (Star Wars, D&D, etc.)
    - Gather reference material for specific settings or time periods
    - Find naming conventions, political structures, cultural details

    The Research Agent will search the web (primarily Wikipedia and fan wikis)
    and return comprehensive material you can mine for world-building ideas.

    Args:
        session_id: Session identifier for the research agent.
        query: What to research. Be specific about what aspects you need.
            Examples:
            - "Research Roman Senate political structure and notable senators"
            - "Research Clone Wars era Jedi Council members and their fates"
            - "Research feudal Japan daimyo system and samurai culture"
            - "Research deep sea bioluminescent creatures and coral reef ecosystems"

    Returns:
        Dictionary with comprehensive research findings organized by source.
    """
    from src.agents.research_agent import ResearchAgent

    agent = ResearchAgent(session_id)
    result = agent.research(query)

    return {"text_response": str(result)}

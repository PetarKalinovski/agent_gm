"""Streaming callback handler for SSE events.

Based on the ToolUsageTracker pattern from dnd_rpg template.
"""

import ast
import json
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Generator


SUBAGENT_TOOLS = frozenset({
    "prompt_npc_agent",
    "prompt_creator_agent",
    "prompt_research_agent",
})


@dataclass
class StreamEvent:
    """A streaming event to send to the client."""
    event_type: str  # "token", "tool_update", "tool_summary", "narration", "complete", "error"
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Convert to SSE format."""
        payload = {
            "type": self.event_type,
            "data": self.data,
            "timestamp": self.timestamp
        }
        return f"data: {json.dumps(payload)}\n\n"


class ToolUsageTracker:
    """Collects tool usage information emitted through agent callbacks.

    Based on the dnd_rpg template implementation.
    """

    def __init__(self) -> None:
        """Initialize trackers for active tools and notification buffers."""
        self._tools: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._notifications: list[dict[str, Any]] = []
        self._spoken_keys: set[tuple[str, str]] = set()
        self._snapshots: dict[str, str] = {}
        self._result_seen = False

    def reset(self) -> None:
        """Clear all accumulated tool usage state."""
        self._tools.clear()
        self._notifications.clear()
        self._snapshots.clear()
        self._result_seen = False

    def __call__(self, **payload: Any) -> None:
        """Receive callback payloads from the Strands agent runtime."""
        self._ingest(payload)

    def process_stream_payload(self, payload: Any) -> None:
        """Process events received through the async iterator.

        The stream_async yields various event types that we need to handle:
        - current_tool_use: Tool is being invoked
        - message: Tool result message
        - result: Final agent result
        - data: Token streaming data
        """
        if not isinstance(payload, dict):
            return
        # Pass the entire payload to _ingest to handle all event types
        self._ingest(payload)

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Return and clear any pending notifications."""
        pending = self._notifications[:]
        self._notifications.clear()
        return pending

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a deep copy of the current tool usage state."""
        return [deepcopy(tool) for tool in self._tools.values()]

    def _ingest(self, payload: Any) -> None:
        """Route incoming agent callbacks to the appropriate handlers."""
        if not isinstance(payload, dict):
            return
        if "current_tool_use" in payload:
            self._handle_tool_use(payload["current_tool_use"])
        if "message" in payload:
            self._handle_tool_result_message(payload["message"])
        if "result" in payload:
            self._handle_result(payload["result"])

    def _handle_tool_use(self, tool_use: Any) -> None:
        """Track metadata about an in-flight tool invocation."""
        if not isinstance(tool_use, dict):
            return
        tool_use_id = tool_use.get("toolUseId")
        tool_name = tool_use.get("name")
        if not tool_use_id or not tool_name:
            return
        tool_id = str(tool_use_id)
        is_subagent = tool_name in SUBAGENT_TOOLS
        entry = self._tools.setdefault(
            tool_id,
            {"id": tool_id, "name": tool_name, "status": "running"},
        )
        entry["name"] = tool_name or entry["name"]
        entry["status"] = entry.get("status") or "running"
        if is_subagent:
            entry["is_subagent"] = True

        # Surface WHO the DM is consulting as soon as the streaming tool
        # input contains the npc_id — the frontend shows "<name> is
        # thinking…" with their portrait instead of a generic spinner
        # during the (slow) NPC-agent hop.
        if tool_name == "prompt_npc_agent" and "npc_id" not in entry:
            tool_input = tool_use.get("input")
            parsed = None
            if isinstance(tool_input, dict):
                parsed = tool_input
            elif isinstance(tool_input, str) and tool_input:
                try:
                    parsed = json.loads(tool_input)
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, dict) and parsed.get("npc_id"):
                entry["npc_id"] = str(parsed["npc_id"])

        self._enqueue_notification(entry)

    def _handle_tool_result_message(self, message: Any) -> None:
        """Capture tool completion notifications from assistant messages.

        Handles Strands message format where content blocks can contain:
        - toolResult: Tool execution results
        - toolUse: Tool invocation (alternative to current_tool_use event)
        """
        if not isinstance(message, dict):
            return
        contents = message.get("content") or []
        for block in contents:
            if not isinstance(block, dict):
                continue

            # Handle toolUse blocks (Strands format in message content)
            if "toolUse" in block:
                tool_use = block["toolUse"]
                if isinstance(tool_use, dict):
                    self._handle_tool_use(tool_use)

            # Handle toolResult blocks
            if "toolResult" not in block:
                continue
            result = block["toolResult"]
            if not isinstance(result, dict):
                continue
            tool_use_id = result.get("toolUseId")
            if not tool_use_id:
                continue
            tool_id = str(tool_use_id)
            entry = self._tools.setdefault(
                tool_id,
                {"id": tool_id, "name": result.get("name"), "status": "running"},
            )
            entry["status"] = result.get("status") or "success"
            if result.get("name"):
                entry["name"] = result["name"]
            if entry.get("name") in SUBAGENT_TOOLS:
                entry["is_subagent"] = True

            # Extract text_response from sub-agent tool results
            if entry.get("is_subagent"):
                tool_content = result.get("content", [])
                for content_block in tool_content:
                    if isinstance(content_block, dict) and "text" in content_block:
                        try:
                            try:
                                parsed = json.loads(content_block["text"])
                            except json.JSONDecodeError:
                                parsed = ast.literal_eval(content_block["text"])
                            if isinstance(parsed, dict) and "text_response" in parsed:
                                entry["output"] = parsed["text_response"]
                        except (json.JSONDecodeError, TypeError, ValueError, SyntaxError):
                            pass

            self._enqueue_notification(entry)

            # Check for special game events in tool output
            tool_content = result.get("content", [])
            for content_block in tool_content:
                # Content blocks may be {'text': "..."} or {'type': 'text', 'text': "..."}
                if isinstance(content_block, dict) and "text" in content_block:
                    self._parse_tool_output_for_events(content_block.get("text", "{}"))

    _CINE_FIELDS = ("kind", "title", "subtitle", "npc_id", "mood", "duration_ms")

    def _push_cinematic(self, data: Any) -> None:
        """Queue a `cinematic` notification from a validated payload dict."""
        if not isinstance(data, dict) or not data.get("kind"):
            return
        note: dict[str, Any] = {"type": "cinematic"}
        note.update({key: data.get(key) for key in self._CINE_FIELDS})
        self._notifications.append(note)

    def _parse_tool_output_for_events(self, text_value: str) -> None:
        """Parse tool output text for special game events like NPC death."""
        try:
            # Try JSON first, then Python literal (single quotes)
            try:
                output_data = json.loads(text_value)
            except json.JSONDecodeError:
                # Tool results often come as Python dict strings with single quotes
                output_data = ast.literal_eval(text_value)

            if not isinstance(output_data, dict):
                return

            # A cinematic can ride along on ANY tool result (start_combat
            # carries the boss intro, campaign ops carry title cards). Pushed
            # BEFORE the tool's own event so the client can defer whatever that
            # event triggers — the boss intro must land ahead of combat_start.
            self._push_cinematic(output_data.get("cinematic"))

            event = output_data.get("event")

            # Detect NPC death events
            if event == "npc_death":
                self._notifications.append({
                    "type": "npc_death",
                    "npc_id": output_data.get("npc_id"),
                    "npc_name": output_data.get("npc_name"),
                    "cause": output_data.get("cause_of_death"),
                })

            # NPC dialogue (from the speak tool) — the server layer enriches
            # this with npc_id + a TTS audio_id before it reaches the client
            elif event == "speech":
                # One speak() call must render one line. Seen live on the
                # LiteLLM/Anthropic path: the same tool result reached the
                # tracker twice and the player read the line twice.
                key = (str(output_data.get("npc") or ""), str(output_data.get("text") or ""))
                if key in self._spoken_keys:
                    return
                self._spoken_keys.add(key)
                note = {
                    "type": "speech",
                    "npc_name": output_data.get("npc"),
                    "text": output_data.get("text"),
                    "tone": output_data.get("tone", "normal"),
                    "action": output_data.get("action"),
                }
                if output_data.get("synthesized"):
                    note["synthesized"] = True
                self._notifications.append(note)

            # Scene description (from describe_location) — music/atmosphere cues
            elif event == "scene":
                self._notifications.append({
                    "type": "scene",
                    "location": output_data.get("location"),
                    "time_of_day": output_data.get("time_of_day", "day"),
                    "atmosphere": output_data.get("atmosphere") or [],
                })

            # Quest lifecycle (activate_quest / update_quest_status) —
            # frontend banners + triumph stinger on completion
            elif event in ("quest_activated", "quest_completed", "quest_failed"):
                self._notifications.append({
                    "type": event,
                    "quest_id": output_data.get("id"),
                    "title": output_data.get("title"),
                    "rewards_applied": output_data.get("rewards_applied") or [],
                })

            # Real-time combat kickoff (start_combat) — frontend spawns
            # the enemies and locks the room. Per-enemy tier/params/boss and
            # the top-level boss object travel through untouched: the server
            # resolved them, the client never recomputes.
            elif event == "combat_start":
                self._notifications.append({
                    "type": "combat_start",
                    "enemies": output_data.get("enemies") or [],
                    "boss": output_data.get("boss"),
                })

            # An objective the engine ticked off (rides tool results as an
            # `events` item; engine paths outside a turn are drained server-side)
            elif event == "objective_done":
                self._notifications.append({
                    "type": "objective_done",
                    "quest_id": output_data.get("quest_id"),
                    "quest_title": output_data.get("quest_title"),
                    "objective_id": output_data.get("objective_id"),
                    "text": output_data.get("text"),
                    "kind": output_data.get("kind"),
                    "all_done": bool(output_data.get("all_done")),
                })

            # Skill check outcome (roll_check) — labels only, never numbers
            elif event == "check_result":
                self._notifications.append({
                    "type": "check_result",
                    "outcome": output_data.get("outcome"),
                    "skill": output_data.get("skill"),
                })

            # Cinematic moment (play_cinematic's own result)
            elif event == "cinematic":
                self._push_cinematic(output_data)

            # Progression rides ALONGSIDE the event, not instead of it — a
            # quest completion carries both `event: "quest_completed"` and a
            # `progression` block, so this runs after the chain, not inside it.
            progression = output_data.get("progression")
            if isinstance(progression, dict) and progression.get("xp_gained"):
                self._notifications.append({
                    "type": "xp_gain",
                    "amount": progression.get("xp_gained"),
                    "source": progression.get("source"),
                    "xp": progression.get("xp"),
                    "level": progression.get("level"),
                    "xp_into_level": progression.get("xp_into_level"),
                    "xp_for_level": progression.get("xp_for_level"),
                })
                if progression.get("leveled_up"):
                    self._notifications.append({
                        "type": "level_up",
                        "level": progression.get("level"),
                        "previous_level": progression.get("previous_level"),
                        "unlocked": progression.get("unlocked") or [],
                        "stats": progression.get("stats") or {},
                    })

            # A tool result may carry a LIST of events too — a sub-agent's
            # dialogue that was written as prose is voiced this way. Each
            # rides exactly the same path as a standalone result.
            for item in output_data.get("events") or []:
                if isinstance(item, dict) and item.get("event"):
                    self._parse_tool_output_for_events(json.dumps(item, default=str))
        except (json.JSONDecodeError, TypeError, ValueError, SyntaxError):
            pass  # Not parseable as dict, ignore

    def _handle_result(self, result: Any) -> None:
        """Emit a final summary once the agent completes its response."""
        if self._tools and not self._result_seen:
            self._result_seen = True
            summary = {"tools": self.snapshot()}
            self._notifications.append({"type": "tool_summary", "payload": summary})

    def _enqueue_notification(self, entry: dict[str, Any]) -> None:
        """Queue a normalized update when a tool entry changes."""
        normalized = self._normalize_entry(entry)
        tool_id = normalized["id"]
        serialized = json.dumps(
            normalized, sort_keys=True, ensure_ascii=False, default=str
        )
        if self._snapshots.get(tool_id) == serialized:
            return
        self._snapshots[tool_id] = serialized
        self._notifications.append({"type": "tool_update", "tool": normalized})

    @staticmethod
    def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Remove noisy fields from tool payloads prior to serialization."""
        normalized = deepcopy(entry)
        normalized.pop("input", None)
        normalized.pop("raw_output", None)
        normalized.pop("error", None)
        # Preserve output only for sub-agent tools (contains the response text)
        if not normalized.get("is_subagent"):
            normalized.pop("output", None)
        return normalized


# Keep StreamEvent for backward compatibility
StreamingCallbackHandler = ToolUsageTracker

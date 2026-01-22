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
        entry = self._tools.setdefault(
            tool_id,
            {"id": tool_id, "name": tool_name, "status": "running"},
        )
        entry["name"] = tool_name or entry["name"]
        entry["status"] = entry.get("status") or "running"
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
            self._enqueue_notification(entry)

            # Check for special game events in tool output
            tool_content = result.get("content", [])
            for content_block in tool_content:
                # Content blocks may be {'text': "..."} or {'type': 'text', 'text': "..."}
                if isinstance(content_block, dict) and "text" in content_block:
                    self._parse_tool_output_for_events(content_block.get("text", "{}"))

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

            # Detect NPC death events
            if output_data.get("event") == "npc_death":
                self._notifications.append({
                    "type": "npc_death",
                    "npc_id": output_data.get("npc_id"),
                    "npc_name": output_data.get("npc_name"),
                    "cause": output_data.get("cause_of_death"),
                })
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
        normalized.pop("output", None)
        normalized.pop("raw_output", None)
        normalized.pop("error", None)
        return normalized


# Keep StreamEvent for backward compatibility
StreamingCallbackHandler = ToolUsageTracker

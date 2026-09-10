"""Execute the tool calls the DM wrote as text.

Seen live: the DM model printed a complete `prompt_npc_agent` call — a
fenced JSON object with npc_id, player_input and context — into its
narration instead of invoking the tool. The player read raw JSON and the
NPC never answered. Prompts cannot fix a model that sometimes forgets how
to call tools, so the engine does what the DM meant: fenced code never
reaches the client (`CodeFenceFilter`), and anything inside it that reads
as a known tool call is executed here after the turn, its speech streamed
as if the call had been real, and the DM told what happened next turn.

Only dialogue tools are rescued (prompt_npc_agent, speak): they are the
calls whose absence the player notices immediately, and executing them
late is harmless. World-mutating calls stay unrescued — a move or a wound
applied a turn late would contradict what the player just read.
"""

import json
import re
from typing import Any

from loguru import logger

from src.models import NPC, Player, get_session

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_LANGUAGE_TAGS = {"", "json", "json5", "javascript", "js", "python", "py", "yaml", "text"}
RESCUABLE = ("prompt_npc_agent", "speak")


def parse_call(blob: str) -> tuple[str, dict[str, Any]] | None:
    """(tool_name, arguments) for a fenced block that reads as a tool call.

    Accepts the bare-arguments form the DM actually wrote (keys alone name
    the tool) and the explicit `{"tool"|"name": ..., "arguments"|"input":
    {...}}` forms. None for anything else — prose, lists, unknown tools.
    """
    text = (blob or "").strip()
    first, _, rest = text.partition("\n")
    if first.strip().lower() in _LANGUAGE_TAGS and rest.strip():
        text = rest
    match = _JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    name = data.get("tool") or data.get("name") or data.get("function")
    args = data.get("arguments") or data.get("input") or data.get("parameters") or data.get("args")
    if isinstance(name, str) and isinstance(args, dict):
        return (name, args) if name in RESCUABLE else None

    keys = set(data)
    if "npc_id" in keys and ("player_input" in keys or "context" in keys):
        return "prompt_npc_agent", data
    if "text" in keys and ("npc_name" in keys or "npc" in keys):
        return "speak", data
    return None


def _tokens(value: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (value or "").lower()) if t}


def resolve_npc(session, ident: str | None, location_id: str | None):
    """An NPC row for an id, a name, or a slug like "brann_coldiron".

    Exact id first; then every NPC whose name contains all the tokens of the
    hint, preferring one standing where the player is.
    """
    if not ident:
        return None
    npc = session.get(NPC, ident)
    if npc:
        return npc
    wanted = _tokens(ident)
    if not wanted:
        return None
    candidates = []
    for row in session.query(NPC).all():
        if wanted <= _tokens(row.name):
            candidates.append(row)
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n.current_location_id != location_id, len(n.name)))
    return candidates[0]


def rescue_tool_calls(
    fences: list[str], player_id: str, addressed_npc_id: str | None = None
) -> list[dict[str, Any]]:
    """Run every rescuable call found in the captured fences, in order.

    Returns one record per executed call:
    {"tool", "args", "npc_id", "npc_name", "events": [speech event dicts],
     "output": str, "note": str}. Nothing is raised — a rescue that fails
    is logged and skipped; the turn already happened.
    """
    from src.tools.agents_as_tools import prompt_npc_agent
    from src.tools.narration import speak

    rescued: list[dict[str, Any]] = []
    for blob in fences or []:
        parsed = parse_call(blob)
        if not parsed:
            continue
        tool_name, args = parsed
        try:
            with get_session() as session:
                player = session.get(Player, player_id)
                location_id = player.current_location_id if player else None
                hint = args.get("npc_id") or args.get("npc_name") or args.get("npc") or addressed_npc_id
                npc = resolve_npc(session, str(hint) if hint else None, location_id)
                npc_id = npc.id if npc else None
                npc_name = npc.name if npc else str(args.get("npc_name") or args.get("npc") or "")

            if tool_name == "speak":
                text = str(args.get("text") or "").strip()
                if not text or not npc_name:
                    continue
                event = speak(npc_name, text, str(args.get("tone") or "normal"), args.get("action"))
                rescued.append({
                    "tool": "speak", "args": args, "npc_id": npc_id, "npc_name": npc_name,
                    "events": [event], "output": "",
                    "note": f"speak({npc_name!r}) was written as text; the engine delivered the line.",
                })
                continue

            if not npc_id:
                logger.warning(f"Tool rescue: could not resolve NPC for {hint!r}")
                continue
            player_input = str(args.get("player_input") or "").strip()
            result = prompt_npc_agent(
                player_id, npc_id, player_input,
                is_first_interaction=bool(args.get("is_first_interaction", False)),
                context=str(args.get("context") or ""),
            )
            if not isinstance(result, dict) or result.get("error"):
                logger.warning(f"Tool rescue: prompt_npc_agent failed: {result}")
                continue
            events = [
                {"event": "speech", "npc": npc_name, "text": line, "tone": "normal", "action": None}
                for line in (result.get("spoken") or [])
            ] + list(result.get("events") or [])
            rescued.append({
                "tool": "prompt_npc_agent", "args": args, "npc_id": npc_id, "npc_name": npc_name,
                "events": events, "output": str(result.get("text_response") or ""),
                "note": (
                    f"prompt_npc_agent for {npc_name} was written as TEXT instead of called. "
                    f"The engine executed it; {npc_name} has already answered the player "
                    f"(lines: {' / '.join(str(e.get('text')) for e in events)[:400]!r}). "
                    "Do not repeat those lines. Call tools — never type them."
                ),
            })
        except Exception as e:  # the turn already happened; never break the stream
            logger.warning(f"Tool rescue failed for {tool_name}: {e}")
    return rescued

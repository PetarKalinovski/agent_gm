"""The DM's history: a window of recent turns plus a written chronicle.

The previous manager never trimmed until the model's context overflowed, so
every turn re-sent every earlier turn — each one carrying its full context
block and every tool result. Input grew without bound across a session.

This manager compacts each turn's context block the moment the turn ends —
the next turn brings a fresh scene brief, so the model only ever reads ONE
current picture of the world, and the history is just the player's words,
the DM's replies and the tool calls. It trims in big steps: when the history
passes MAX_TURNS it is cut back to KEEP_TURNS in one go (one prompt-cache
miss per cut instead of one per turn). What the cut removed is folded, in
the background, into the character's `chronicle.md` (`memory_files`) — a
few paragraphs of "the story so far" the scene context carries every turn.
The facts themselves (who is dead, who trusts whom, which quests are open)
live in the database and reach the DM through the scene brief, so nothing
mechanical depends on the transcript.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

from strands.agent.conversation_manager import ConversationManager

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

MAX_TURNS = 30
KEEP_TURNS = 15
CONTEXT_MARKER = "Player says:"

# Older managers this one may inherit a session from
_COMPATIBLE_STATES = {"SemanticSummarizingConversationManager", "SlidingWindowConversationManager"}


def _is_turn_start(message: dict) -> bool:
    """A user message that carries text (a player turn), not a tool result."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content") or []
    return any(isinstance(b, dict) and "text" in b for b in content) and not any(
        isinstance(b, dict) and "toolResult" in b for b in content
    )


def turn_starts(messages: list) -> list[int]:
    return [i for i, m in enumerate(messages or []) if _is_turn_start(m)]


def compact_context_text(text: str) -> str:
    """'Current context: … Player says: X' -> a one-line stamp + the words."""
    if CONTEXT_MARKER not in text:
        return text
    head, said = text.rsplit(CONTEXT_MARKER, 1)
    where = re.search(r"Where: ([^\[\n]+)", head) or re.search(r"- Location: ([^\n]+)", head)
    when = re.search(r"(Day \d+, \d{1,2}:\d{2})", head)
    stamp = ", ".join(s for s in ((where.group(1).strip() if where else ""),
                                   (when.group(1) if when else "")) if s)
    return f"[earlier turn{' — ' + stamp if stamp else ''}] {CONTEXT_MARKER} {said.strip()}"


def compact_message(message: dict) -> None:
    """In place: the context block of an old turn shrinks to its stamp."""
    for block in message.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"] = compact_context_text(block["text"])


def transcript_of(messages: list) -> str:
    """Plain text of what was said and done, for the chronicle writer."""
    lines: list[str] = []
    for m in messages or []:
        role = m.get("role")
        for block in m.get("content") or []:
            if not isinstance(block, dict):
                continue
            if "text" in block and role == "user":
                text = block["text"]
                if CONTEXT_MARKER in text:
                    text = text.rsplit(CONTEXT_MARKER, 1)[1]
                lines.append(f"PLAYER: {text.strip()[:400]}")
            elif "text" in block and role == "assistant":
                lines.append(f"DM: {block['text'].strip()[:1200]}")
            elif "toolUse" in block:
                tu = block["toolUse"]
                lines.append(f"(tool {tu.get('name')} {str(tu.get('input'))[:160]})")
    return "\n".join(line for line in lines if line.strip())


class TurnWindowManager(ConversationManager):
    """Keep recent turns; trim old ones in steps; hand them to a chronicler."""

    def __init__(self, on_trim: Callable[[list], None] | None = None,
                 max_turns: int = MAX_TURNS, keep_turns: int = KEEP_TURNS) -> None:
        super().__init__()
        self.on_trim = on_trim
        self.max_turns = max_turns
        self.keep_turns = keep_turns

    def restore_from_session(self, state: dict[str, Any]) -> Optional[list]:
        # A session written by an older manager is fine: only the offset matters
        name = state.get("__name__")
        if name != self.__class__.__name__ and name not in _COMPATIBLE_STATES:
            raise ValueError("Invalid conversation manager state.")
        self.removed_message_count = int(state.get("removed_message_count") or 0)
        return None

    def apply_management(self, agent: "Agent", **kwargs: Any) -> None:
        # Runs once the invocation is over: this turn's context is spent
        for index in turn_starts(agent.messages):
            compact_message(agent.messages[index])
        if len(turn_starts(agent.messages)) > self.max_turns:
            self._trim(agent, self.keep_turns)

    def reduce_context(self, agent: "Agent", e: Optional[Exception] = None, **kwargs: Any) -> None:
        starts = turn_starts(agent.messages)
        self._trim(agent, max(2, min(self.keep_turns, len(starts) // 2)))

    def _trim(self, agent: "Agent", keep: int) -> None:
        starts = turn_starts(agent.messages)
        if len(starts) <= keep:
            return
        cut = starts[-keep]
        dropped = list(agent.messages[:cut])
        agent.messages[:] = agent.messages[cut:]
        self.removed_message_count += cut
        logger.info(f"DM history trimmed: dropped {cut} messages, kept {keep} turns")
        if self.on_trim and dropped:
            try:
                self.on_trim(dropped)
            except Exception as e:  # the chronicle is a nicety, never a crash
                logger.warning(f"chronicle hand-off failed: {e}")


def compact_loaded_history(messages: list) -> None:
    """On load (a session written before compaction existed): stamps only."""
    for index in turn_starts(messages):
        compact_message(messages[index])


CHRONICLE_PROMPT = """You keep the chronicle of one character's story in a text RPG.
You get the chronicle so far and a transcript of the turns that just left the
game master's short-term memory. Rewrite the chronicle to include them.

Rules:
- At most 5 short paragraphs, past tense, second person ("you").
- Keep what a game master needs to stay consistent: promises made and to whom,
  debts, insults, who helped or betrayed the player, what they learned, where
  they have been, unresolved threads, the tone of key relationships.
- Drop moment-to-moment description. No numbers, no game mechanics.
- Write ONLY the chronicle text."""

CHRONICLE_MAX_CHARS = 3000


def _silent(**_kwargs: Any) -> None:
    """The chronicler runs beside a live turn: create_agent would otherwise
    fall back to the global callback handler and stream into that turn."""


def write_chronicle(player_id: str, dropped: list) -> None:
    """Fold trimmed turns into the character's chronicle.md with the fast model."""
    transcript = transcript_of(dropped)
    if not transcript.strip():
        return
    from src.services import memory_files

    previous = memory_files.read_chronicle(player_id)
    try:
        from src.agents.base import create_agent

        writer = create_agent("recap", CHRONICLE_PROMPT, tools=[], callback_handler=_silent)
        reply = str(writer(
            f"CHRONICLE SO FAR:\n{previous or '(nothing yet)'}\n\nTURNS TO FOLD IN:\n{transcript[-12000:]}"
        )).strip()
    except Exception as e:
        logger.warning(f"chronicle writer failed: {e}")
        reply = ""
    if not reply:
        # Never lose the thread entirely: keep the player's own words
        said = [line for line in transcript.splitlines() if line.startswith("PLAYER:")]
        reply = (previous + "\n" + " / ".join(s[8:120] for s in said[-8:])).strip()
    memory_files.write_chronicle(player_id, reply[-CHRONICLE_MAX_CHARS:])
    logger.info(f"Chronicle updated for {player_id} ({len(reply)} chars)")


def chronicle_hand_off(player_id: str) -> Callable[[list], None]:
    """on_trim callback: write the chronicle on a background thread."""
    def hand_off(dropped: list) -> None:
        threading.Thread(target=write_chronicle, args=(player_id, dropped), daemon=True).start()
    return hand_off

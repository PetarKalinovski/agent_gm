"""The terminal's view of a DM reply: prose printed, lines spoken, commands applied.

The web server runs the DM's streamed reply through the speaker-line and
command filters (src/web/server.py play_sse). The CLI needs the same three
channels, so this callback handler does it for the terminal: prose streams
to the console, speaker lines print as "Name: "words"" in colour, and
`@command` lines are applied through the same CommandRunner and never shown.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.text import Text

from src.web.dialogue_markup import DialogueMarkupFilter


class CliReplyHandler:
    """Strands callback handler for one CLI session's DM."""

    def __init__(self, console: Console, player_id: str) -> None:
        self.console = console
        self.player_id = player_id
        self.markup = DialogueMarkupFilter(accept_unquoted=lambda speaker, tone: bool(tone))
        self.runner = None
        self.speeches: list[dict[str, Any]] = []
        self.tracker = None

    def start_turn(self, present_ids: list[str]) -> None:
        from src.services.commands import CommandRunner
        from src.web.streaming import ToolUsageTracker

        self.runner = CommandRunner(self.player_id, present_ids=present_ids)
        self.speeches = []
        # The dice this reply, as the web path reads them: a road barred for
        # the night opens to a won roll (commands._won_the_night)
        self.tracker = ToolUsageTracker()

    def __call__(self, **kwargs: Any) -> None:
        if self.tracker is not None and self.runner is not None:
            self.tracker.process_stream_payload(kwargs)
            for note in self.tracker.drain_notifications():
                if note.get("type") == "check_result" and note.get("outcome"):
                    self.runner.roll_outcomes.append(str(note["outcome"]))
        data = kwargs.get("data")
        if data:
            self._render(self.markup.feed_segments(data))
        if kwargs.get("current_tool_use"):
            self._render(self.markup.flush_segments())

    def finish_turn(self) -> list[str]:
        """Flush the last line; return the engine's ledger for the next turn."""
        self._render(self.markup.flush_segments())
        return self.runner.ledger() if self.runner else []

    def _render(self, segments: list[tuple[str, Any]]) -> None:
        for kind, value in segments:
            if kind == "text":
                self.console.print(value, end="", markup=False, highlight=False)
            elif kind == "speech":
                self.speeches.append(value)
                line = Text(f"{value['speaker']}: ", style="bold cyan")
                line.append(f"\"{value['text']}\"", style="white")
                if value.get("action"):
                    self.console.print(Text(f"*{value['action']}*", style="dim italic"))
                self.console.print(line)
            elif kind == "command" and self.runner is not None:
                outcome = self.runner.run(value)
                if not outcome.ok and outcome.message:
                    self.console.print(Text(f"[engine] {outcome.message}", style="dim"))

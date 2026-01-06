"""Tools for narration and output to the player."""

from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from strands import tool

# Global console for rich output
_console: Console | None = None


def get_console() -> Console:
    """Get or create the console for output."""
    global _console
    if _console is None:
        _console = Console()
    return _console


def set_console(console: Console) -> None:
    """Set the console for output (useful for testing)."""
    global _console
    _console = console


@tool
def narrate(text: str, style: str = "narrative") -> dict[str, Any]:
    """Output narrative text to the player.

    Args:
        text: The narrative text to display.
        style: Style of narration (narrative, action, system, whisper).

    Returns:
        Dictionary confirming the narration.
    """
    console = get_console()

    if style == "narrative":
        console.print(Markdown(text))
    elif style == "action":
        console.print(Text(text, style="bold yellow"))
    elif style == "system":
        console.print(Panel(text, title="System", border_style="dim"))
    elif style == "whisper":
        console.print(Text(text, style="dim italic"))
    else:
        console.print(text)

    return {"success": True, "style": style, "length": len(text)}


@tool
def speak(
    npc_name: str,
    text: str,
    tone: str = "normal",
    action: str | None = None
) -> dict[str, Any]:
    """Output NPC dialogue to the player.

    Args:
        npc_name: The name of the speaking NPC.
        text: The dialogue text.
        tone: Tone of speech (normal, whispered, shouted, nervous, angry).
        action: Optional physical action while speaking.

    Returns:
        Dictionary confirming the speech.
    """
    console = get_console()

    # Format based on tone
    tone_styles = {
        "normal": "white",
        "whispered": "dim italic",
        "shouted": "bold red",
        "nervous": "yellow",
        "angry": "bold magenta",
        "friendly": "green",
        "suspicious": "cyan italic",
    }

    style = tone_styles.get(tone, "white")

    # Build the output
    if action:
        console.print(Text(f"*{action}*", style="dim italic"))

    # Create speaker label with dialogue
    speaker = Text(f"{npc_name}: ", style="bold cyan")
    dialogue = Text(f'"{text}"', style=style)

    console.print(speaker + dialogue)

    return {"success": True, "npc": npc_name, "tone": tone}


@tool
def describe_location(
    name: str,
    description: str,
    atmosphere: list[str] | None = None,
    npcs_visible: list[str] | None = None,
    time_of_day: str = "day"
) -> dict[str, Any]:
    """Display a location description to the player.

    Args:
        name: Location name.
        description: Location description.
        atmosphere: List of atmosphere tags.
        npcs_visible: List of NPC names visible here.
        time_of_day: Current time of day.

    Returns:
        Dictionary confirming the description.
    """
    console = get_console()

    # Time-based styling
    time_styles = {
        "morning": "yellow",
        "afternoon": "white",
        "evening": "orange3",
        "night": "blue",
    }
    border_style = time_styles.get(time_of_day, "white")

    # Build content
    content = description

    if npcs_visible:
        content += f"\n\n*You see: {', '.join(npcs_visible)}*"

    console.print(Panel(
        Markdown(content),
        title=f"[bold]{name}[/bold]",
        subtitle=f"[dim]{time_of_day}[/dim]",
        border_style=border_style,
    ))

    return {"success": True, "location": name}


@tool
def show_combat_action(
    actor: str,
    action: str,
    target: str | None = None,
    result: str = "hit",
    dramatic: bool = False
) -> dict[str, Any]:
    """Display a combat action to the player.

    Args:
        actor: Who is acting.
        action: What they're doing.
        target: Who they're targeting.
        result: Result of the action (hit, miss, critical, blocked).
        dramatic: Whether this is a dramatic moment.

    Returns:
        Dictionary confirming the display.
    """
    console = get_console()

    result_styles = {
        "hit": "yellow",
        "miss": "dim",
        "critical": "bold red",
        "blocked": "cyan",
        "devastating": "bold magenta",
    }

    style = result_styles.get(result, "white")

    if target:
        text = f"{actor} {action} {target}!"
    else:
        text = f"{actor} {action}!"

    if dramatic:
        console.print(Panel(
            Text(text, style=style, justify="center"),
            border_style="red",
        ))
    else:
        console.print(Text(f"  ⚔ {text}", style=style))

    return {"success": True, "result": result}


@tool
def show_status_change(
    entity: str,
    status_type: str,
    old_value: str,
    new_value: str
) -> dict[str, Any]:
    """Display a status change to the player.

    Args:
        entity: Who or what changed.
        status_type: Type of status (health, mood, reputation).
        old_value: Previous value.
        new_value: New value.

    Returns:
        Dictionary confirming the display.
    """
    console = get_console()

    # Determine if this is positive or negative
    negative_words = ["hurt", "badly", "critical", "hostile", "angry", "worse"]
    is_negative = any(word in new_value.lower() for word in negative_words)

    arrow = "↓" if is_negative else "↑"
    style = "red" if is_negative else "green"

    text = f"{entity}'s {status_type}: {old_value} {arrow} {new_value}"
    console.print(Text(f"  [{text}]", style=style))

    return {"success": True, "change": new_value}


@tool
def show_time_passage(hours: float, description: str = "") -> dict[str, Any]:
    """Display time passing to the player.

    Args:
        hours: Number of hours that passed.
        description: Optional description of what happened.

    Returns:
        Dictionary confirming the display.
    """
    console = get_console()

    if hours < 1:
        time_text = f"{int(hours * 60)} minutes"
    elif hours == 1:
        time_text = "1 hour"
    else:
        time_text = f"{hours:.1f} hours"

    text = f"⏳ {time_text} pass..."
    if description:
        text += f" {description}"

    console.print(Text(text, style="dim italic"))

    return {"success": True, "hours": hours}


@tool
def prompt_player(prompt: str, choices: list[str] | None = None) -> dict[str, Any]:
    """Display a prompt to the player (for system use, actual input handled elsewhere).

    Args:
        prompt: The prompt text.
        choices: Optional list of choices.

    Returns:
        Dictionary confirming the prompt was shown.
    """
    console = get_console()

    console.print()
    if choices:
        console.print(Text(prompt, style="bold"))
        for i, choice in enumerate(choices, 1):
            console.print(Text(f"  {i}. {choice}", style="cyan"))
    else:
        console.print(Text(f"➤ {prompt}", style="bold green"))

    return {"success": True, "has_choices": bool(choices)}


@tool
def show_quest_update(
    title: str,
    update_type: str,
    description: str | None = None,
    objectives: list[str] | None = None,
) -> dict[str, Any]:
    """Display a quest update notification to the player.

    Args:
        title: Quest title.
        update_type: Type of update (started, updated, completed, failed).
        description: Optional quest description (shown for new quests).
        objectives: Optional list of objectives to display.

    Returns:
        Dictionary confirming the display.
    """
    console = get_console()

    type_styles = {
        "started": ("bold green", "📜 NEW QUEST"),
        "updated": ("yellow", "📝 QUEST UPDATED"),
        "completed": ("bold cyan", "✅ QUEST COMPLETED"),
        "failed": ("bold red", "❌ QUEST FAILED"),
    }

    style, header = type_styles.get(update_type, ("white", "📜 QUEST"))

    content = f"**{title}**"
    if description:
        content += f"\n\n{description}"
    if objectives:
        content += "\n\n**Objectives:**"
        for obj in objectives:
            content += f"\n• {obj}"

    console.print(Panel(
        Markdown(content),
        title=f"[{style}]{header}[/{style}]",
        border_style=style.replace("bold ", ""),
    ))

    return {"success": True, "quest": title, "type": update_type}

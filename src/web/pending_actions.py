"""Between-turn action ledger — what the ENGINE did while the DM wasn't looking.

Canvas travel, object pickups and reads, potions drunk from the HUD, gear
swapped, a level earned in a canvas fight: all of it commits between DM turns,
so the DM has no way of knowing it happened. Two consumers close that gap:

1. `format_pending_actions` renders the `[SINCE YOUR LAST TURN]` context block
   so the DM narrates around what is already true (and never re-applies it).
2. `tool_equivalents` names the tools the engine ran on the player's behalf;
   `turn_audit.audit_turn(code_actions=...)` unions them into the succeeded
   set so the reality audit does not flag the DM for narrating them.

Process-local and in-memory, mirroring `_pending_audit_feedback` in server.py:
drained at context-build time under the play lock. If the turn then crashes the
entries are lost — the mutations themselves are already committed, so the only
cost is one less-informed turn.
"""

import threading
from typing import Any, Iterable

# Oldest entries past this are dropped; a wall of notes eats the turn
MAX_PENDING = 12

# Every kind the ledger understands. `omitted` is the internal cap marker.
KINDS = (
    "move", "take", "read", "open", "search", "combat",
    "equip", "consume", "levelup", "objective",
)

# player_id -> list of {"kind", "summary", "tools", ...extra}
_pending: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()

# Tools the engine ALWAYS runs for a kind. Kinds whose mutations are
# conditional (a search that found nothing, a fight nobody was hurt in) carry
# their tools per-entry instead, passed by the caller at record time.
_KIND_TOOLS: dict[str, set[str]] = {
    "move": {"move_player", "advance_time"},
    "consume": {"use_item", "update_player_health"},
}

# Bullet prefixes for the canvas kinds. `combat` is deliberately absent: the
# auto-sent [COMBAT OUTCOME] turn already tells that story.
_BULLET_LABELS = {
    "take": "Picked up",
    "read": "Read",
    "open": "Opened",
    "search": "Searched",
    "objective": "Objective met",
}

# Every objective on a quest is ticked. Completion is still the DM's call —
# the engine never pays a quest out on its own.
QUEST_READY_NOTE = (
    'Every objective on "{quest}" is now met. The player has done the work. '
    "Decide whether it is finished — if it is, call update_quest_status."
)

_HEADER = (
    "[SINCE YOUR LAST TURN the player acted on the canvas. The engine ALREADY\n"
    "applied every line below to the database — the moves, the time, the items.\n"
    "Do NOT call move_player, advance_time, or add_to_inventory for any of it,\n"
    "and do NOT narrate them doing it; they watched themselves do it."
)

# Code-owned note lines. Growth is the engine's business and must never be
# named mechanically, so the level-up note carries its own instruction.
LEVEL_UP_NOTE = (
    "They have grown from what they survived. Acknowledge it ONCE, obliquely — "
    'never say "level", "XP", or a number.'
)

_SLOT_VERBS = {"weapon": "wield", "armor": "wear", "trinket": "carry"}


def equip_note(name: str, slot: str = "weapon", *, automatic: bool = False,
               reads_as: str | None = None) -> str:
    """`They now wield Iron Sword.` / auto-equip variant for a fresh pickup.

    reads_as: the player's NEW `read_as` string (see
        `src/services/perception.py`), passed ONLY when equipping this piece
        actually changed how strangers read them. Armor only; a swap between
        two plain coats says nothing and adds nothing.
    """
    verb = _SLOT_VERBS.get((slot or "").lower(), "carry")
    if automatic:
        note = f"They immediately {verb} the {name} they just got."
    else:
        note = f"They now {verb} {name}."
    if reads_as:
        note += f" To strangers they now read as {reads_as}."
    return note


def unequip_note(name: str) -> str:
    return f"They put away their {name}."


def consume_note(name: str, remaining: int, *, effect: str = "heal_light",
                 in_combat: bool = False) -> str:
    """`They drank a Healing Draught mid-fight, and look steadier (2 left).`"""
    where = " mid-fight" if in_combat else ""
    if effect == "surge":
        return f"They drank a {name} — for the next moments they hit harder ({remaining} left)."
    return f"They drank a {name}{where}, and look steadier ({remaining} left)."


def record_action(player_id: str, kind: str, summary: str,
                  tools: Iterable[str] = (), **extra: Any) -> None:
    """Queue one engine-resolved action for the player's next DM turn.

    `kind` is one of KINDS. `tools` names the world-write tools the engine
    already ran (conditional ones only — see _KIND_TOOLS). Extra keyword
    arguments ride along on the entry: `minutes` and `first_visit` for moves.
    """
    if not player_id:
        return
    entry: dict[str, Any] = {
        "kind": kind,
        "summary": summary or "",
        "tools": sorted({t for t in (tools or ()) if t}),
    }
    entry.update(extra)
    with _lock:
        queue = _pending.setdefault(player_id, [])
        queue.append(entry)
        # Drop the oldest real entries past the cap, leaving a sticky marker
        # so the DM knows the list is partial
        real = [e for e in queue if e.get("kind") != "omitted"]
        if len(real) > MAX_PENDING:
            trimmed = real[len(real) - MAX_PENDING:]
            _pending[player_id] = [{"kind": "omitted", "summary": "", "tools": []}] + trimmed


def drain(player_id: str) -> list[dict[str, Any]]:
    """Pop and return everything queued for this player."""
    with _lock:
        return _pending.pop(player_id, [])


def clear(player_id: str) -> None:
    """Forget queued actions (world switch, player deletion)."""
    with _lock:
        _pending.pop(player_id, None)


def tool_equivalents(actions: list[dict[str, Any]]) -> set[str]:
    """Tool names the ENGINE already ran on the player's behalf.

    Fed to `audit_turn(code_actions=...)` so narrating them is not flagged.
      move                      -> {"move_player", "advance_time"}
      take/open/search w/ items -> {"add_to_inventory"}
      consume                   -> {"use_item", "update_player_health"}
      combat                    -> whatever the result applied
      read / equip / levelup    -> set()
    """
    names: set[str] = set()
    for action in actions or []:
        names |= _KIND_TOOLS.get(action.get("kind", ""), set())
        names |= {t for t in (action.get("tools") or ()) if t}
    return names


def _move_chain(run: list[dict[str, Any]]) -> list[str]:
    """`A -> B`, `B -> C` collapse into `[A, B, C]`."""
    chain: list[str] = []
    for action in run:
        hops = [h.strip() for h in str(action.get("summary", "")).split("->") if h.strip()]
        if not hops:
            continue
        if chain and hops[0] == chain[-1]:
            hops = hops[1:]
        chain.extend(hops)
    return chain


def _format_move_run(run: list[dict[str, Any]]) -> str:
    chain = _move_chain(run)
    if not chain:
        return ""
    minutes = 0
    for action in run:
        try:
            minutes += int(action.get("minutes") or 0)
        except (TypeError, ValueError):
            continue
    line = "- Travelled: " + " -> ".join(chain)
    if minutes > 0:
        line += f" ({minutes} min passed)"
    return line


def format_pending_actions(actions: list[dict[str, Any]]) -> str:
    """The `[SINCE YOUR LAST TURN]` context block. Empty when nothing to report."""
    if not actions:
        return ""

    entries = [a for a in actions if a.get("kind") != "omitted"]
    lines: list[str] = []
    if len(entries) != len(actions):
        lines.append("- (earlier canvas actions omitted)")

    i = 0
    while i < len(entries):
        action = entries[i]
        kind = action.get("kind")
        if kind == "move":
            run = []
            while i < len(entries) and entries[i].get("kind") == "move":
                run.append(entries[i])
                i += 1
            line = _format_move_run(run)
            if line:
                lines.append(line)
            continue
        i += 1
        summary = str(action.get("summary") or "").strip()
        if kind == "levelup":
            lines.append(f"- {LEVEL_UP_NOTE}")
        elif kind in ("equip", "consume"):
            if summary:
                lines.append(f"- {summary}")
        elif kind in _BULLET_LABELS and summary:
            lines.append(f"- {_BULLET_LABELS[kind]}: {summary}")
            if kind == "objective" and action.get("all_done"):
                lines.append(f"- {QUEST_READY_NOTE.format(quest=action.get('quest_title') or 'the quest')}")
        # `combat` (and anything unknown) contributes tools but no prose

    if not lines:
        return ""

    # The describe_location nudge rides on the LAST move only
    tail = ""
    moves = [a for a in entries if a.get("kind") == "move"]
    if moves and moves[-1].get("first_visit"):
        destination = (_move_chain([moves[-1]]) or [""])[-1]
        if destination:
            tail = (
                f"\nThey are now in {destination}, which they have never been described."
                "\nCall describe_location for it before anything else."
            )

    return _HEADER + "\n" + "\n".join(lines) + tail + "]"

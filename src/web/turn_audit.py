"""Post-turn fiction/database consistency audit.

The #1 DM failure class found in self-playtests: narration claiming
mechanics that never happened (quest banners with no quest tool, damage
with no health change, items that never entered the inventory). The
REALITY CONTRACT prompt section fights it at the source; this auditor
catches what slips through — warnings are logged and attached to the
`complete` SSE event so self-playtests get a scoreboard.

High-precision checks only: a false alarm every turn would train everyone
to ignore the audit.
"""

import re
from typing import Any


_QUEST_CLAIM = re.compile(
    r"\b(?:quest\s+(?:accepted|activated|updated|complete[d]?|failed)"
    r"|new\s+quest|QUEST\s+(?:ACCEPTED|ACTIVATED|COMPLETED?|FAILED))\b",
    re.IGNORECASE,
)
_QUEST_TOOLS = {"activate_quest", "update_quest_status", "create_quest", "update_quest_objectives"}

_DAMAGE_CLAIM = re.compile(
    # Second OR first person — playtest caught the DM narrating injuries as
    # "I've pulled something in my shoulder" with zero tool calls
    r"\b(?:you|i)(?:'re|'ve| are| am| have| take| took| suffer(?:ed)?)\b[^.!?\n]{0,50}"
    r"\b(?:damage|wounded?|bleeding|injur(?:ed|y)|hurt(?:\s+now)?"
    r"|pulled something|sprained|twisted my|twisted your|broken (?:rib|arm|leg|ankle|wrist))\b",
    re.IGNORECASE,
)

_ITEM_CLAIM = re.compile(
    r"\b(?:added to your (?:pack|inventory|satchel)"
    r"|you (?:pocket|stow|tuck away|now carry))\b",
    re.IGNORECASE,
)
_ITEM_TOOLS = {"add_to_inventory", "remove_from_inventory", "transfer_item"}

_ARRIVAL_CLAIM = re.compile(
    r"\byou (?:arrive (?:at|in)|reach the|step into|pass through the gates? of)\b",
    re.IGNORECASE,
)

# Meta-mechanics claims ("Consequences applied: ... disposition is now
# hostile ... an event is created") — live playtest caught the DM printing
# a fabricated consequence ledger with zero tool calls
_CONSEQUENCE_CLAIM = re.compile(
    r"(?:consequences applied|disposition is now|relationship is (?:now )?(?:severely )?damaged"
    r"|reputation (?:with [^.!?\n]{0,30})?(?:drops|falls|is damaged|takes a hit)"
    r"|an event [^.!?\n]{0,50}is (?:created|recorded))",
    re.IGNORECASE,
)
_CONSEQUENCE_TOOLS = {
    "update_npc_relationship", "update_npc_mood", "update_player_reputation",
    "create_event", "update_faction",
}


def audit_turn(
    turn_text: str,
    tools: list[dict[str, Any]],
    state_before: dict[str, Any] | None,
    state_after: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Compare what the narration claims against what actually happened.

    Returns a list of {check, detail} warnings (empty = clean turn).
    """
    warnings: list[dict[str, str]] = []
    if not turn_text:
        return warnings

    succeeded = {
        t.get("name")
        for t in (tools or [])
        if t.get("status") == "success" and t.get("name")
    }
    before = state_before or {}
    after = state_after or {}

    if _QUEST_CLAIM.search(turn_text) and not (succeeded & _QUEST_TOOLS):
        warnings.append({
            "check": "quest_banner_without_tool",
            "detail": "Narration announces a quest change but no quest tool succeeded this turn.",
        })

    health_changed = before.get("health") != after.get("health")
    if (
        _DAMAGE_CLAIM.search(turn_text)
        and "update_player_health" not in succeeded
        and not health_changed
    ):
        warnings.append({
            "check": "damage_without_tool",
            "detail": "Narration says the player was hurt but health never changed in the database.",
        })

    inventory_changed = before.get("inventory") != after.get("inventory")
    if _ITEM_CLAIM.search(turn_text) and not (succeeded & _ITEM_TOOLS) and not inventory_changed:
        warnings.append({
            "check": "item_without_tool",
            "detail": "Narration says an item entered the inventory but the inventory never changed.",
        })

    location_changed = before.get("location_id") != after.get("location_id")
    if _ARRIVAL_CLAIM.search(turn_text) and not location_changed and "move_player" not in succeeded:
        warnings.append({
            "check": "arrival_without_move",
            "detail": "Narration describes arriving somewhere but the player's location never changed.",
        })

    if _CONSEQUENCE_CLAIM.search(turn_text) and not (succeeded & _CONSEQUENCE_TOOLS):
        warnings.append({
            "check": "consequences_without_tools",
            "detail": (
                "Narration claims relationship/reputation/event consequences but no "
                "relationship, reputation, or event tool succeeded this turn."
            ),
        })

    return warnings


def format_audit_feedback(warnings: list[dict[str, str]]) -> str:
    """Context block injected into the DM's NEXT turn so it self-corrects."""
    if not warnings:
        return ""
    lines = "\n".join(f"- {w['detail']}" for w in warnings)
    return (
        "\n\n[REALITY AUDIT of your previous turn — the engine found narration "
        "claiming mechanics that never happened:\n"
        f"{lines}\n"
        "The database did NOT change for these claims. THIS turn, either make "
        "them real with the proper tool calls or stop asserting them. Never "
        "print mechanics or consequence lists in narration.]"
    )
